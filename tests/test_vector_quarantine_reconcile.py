import hashlib
import importlib.util
import json
import io
import copy
import contextlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "reconcile_vector_quarantine", SCRIPTS / "reconcile_vector_quarantine.py"
)
reconcile_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconcile_module)


class VectorQuarantineReconcileTests(unittest.TestCase):
    def write_jsonl(self, folder, name, records):
        path = Path(folder, name)
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return path

    def snapshot(self, folder):
        records = [
            {"kind": "header", "started_at": 1.0, "purpose": "read-only bounded vector audit"},
            {"kind": "row", "table": "messages", "id": "m1", "text": "", "vector": json.dumps([1.0] + [0.0] * 383)},
            {"kind": "table_summary", "table": "messages", "before": 1, "exported": 1, "after": 1},
            {"kind": "row", "table": "tool_results", "id": "t1", "text": "synthetic t1", "vector": json.dumps([2.0] + [0.0] * 383)},
            {"kind": "table_summary", "table": "tool_results", "before": 1, "exported": 1, "after": 1},
            {"kind": "footer", "finished_at": 2.0, "complete": True},
        ]
        return self.write_jsonl(folder, "snapshot.jsonl", records)

    def journal(self, folder, name, digest, identities, footer_stage="complete", pending=None):
        records = [{"stage": "header", "snapshot_sha256": digest, "planned": 2, "backup_path": "synthetic-backup", "time": 3.0}]
        for table, identifier in identities:
            records.append({"stage": "intent", "table": table, "id": identifier})
            if pending != (table, identifier):
                records.append({"stage": "applied", "table": table, "id": identifier})
        records.append({"stage": footer_stage, "changed": len(identities), "time": 4.0})
        return self.write_jsonl(folder, name, records)

    def test_valid_completed_and_pilot_overlap_report_aggregates(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = self.snapshot(folder)
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            full = self.journal(folder, "full.jsonl", digest, [("messages", "m1"), ("tool_results", "t1")])
            pilot = self.journal(folder, "pilot.jsonl", digest, [("messages", "m1")], "batch_complete")
            report = reconcile_module.reconcile(snapshot, full, pilot)
            self.assertEqual(report["processed"], 2)
            self.assertEqual(report["applied"], 2)
            self.assertEqual(report["skipped"], 0)
            self.assertEqual(report["pending"], 0)
            self.assertEqual(report["pilot_overlap_full"], 1)
            self.assertNotIn("m1", json.dumps(report))

    def test_active_partial_journal_fails_without_assuming_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = self.snapshot(folder)
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            active = self.journal(folder, "active.jsonl", digest, [("messages", "m1")])
            lines = active.read_text(encoding="utf-8").splitlines()[:-1]
            active.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                reconcile_module.reconcile(snapshot, active)

    def test_pending_intent_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = self.snapshot(folder)
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            pending = self.journal(folder, "pending.jsonl", digest, [("messages", "m1")], pending=("messages", "m1"))
            with self.assertRaisesRegex(ValueError, "pending"):
                reconcile_module.reconcile(snapshot, pending)

    def test_duplicate_mismatched_identity_footer_and_hash_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = self.snapshot(folder)
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            cases = []
            duplicate = self.journal(folder, "duplicate.jsonl", digest, [("messages", "m1"), ("tool_results", "t1")])
            duplicate_lines = duplicate.read_text().splitlines()
            duplicate_lines.insert(2, duplicate_lines[1])
            duplicate.write_text("\n".join(duplicate_lines) + "\n")
            cases.append(duplicate)
            cases.append(self.journal(folder, "mismatch.jsonl", digest, [("tool_results", "t1")]))
            bad_footer = self.journal(folder, "footer.jsonl", digest, [("messages", "m1"), ("tool_results", "t1")], "batch_complete")
            cases.append(bad_footer)
            cases.append(self.journal(folder, "hash.jsonl", "0" * 64, [("messages", "m1"), ("tool_results", "t1")]))
            for journal in cases:
                with self.subTest(journal=journal.name):
                    with self.assertRaises(ValueError):
                        reconcile_module.reconcile(snapshot, journal)

    def test_sequential_pairs_and_footer_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = self.snapshot(folder)
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            journal = self.journal(folder, "full.jsonl", digest, [("messages", "m1"), ("tool_results", "t1")])
            records = [json.loads(line) for line in journal.read_text().splitlines()]
            cases = {
                "multiple_pending": [records[index] for index in (0, 1, 3, 2, 4, 5)],
                "out_of_order_outcomes": [records[index] for index in (0, 1, 4, 3, 2, 5)],
                "duplicate_outcome": records[:3] + [records[2]] + records[3:],
                "duplicate_pair": records[:3] + records[1:3] + records[3:],
                "no_intent": [records[0], *records[2:]],
                "missing_footer": records[:-1],
                "short_complete": records[:3] + [{"stage": "complete", "changed": 1}],
                "after_footer": records + [records[-1]],
                "halted": records[:2] + [{"stage": "halted", "error_type": "SyntheticError"}],
            }
            for key, value in (("changed", True), ("changed", -1), ("changed", 1), ("skipped", None)):
                altered = copy.deepcopy(records)
                altered[-1][key] = value
                cases[str((key, value))] = altered
            for planned in (1, 3, True):
                altered = copy.deepcopy(records)
                altered[0]["planned"] = planned
                cases[str(planned)] = altered
            for name, invalid in cases.items():
                with self.subTest(name=name):
                    self.write_jsonl(folder, "full.jsonl", invalid)
                    with self.assertRaises(ValueError):
                        reconcile_module.reconcile(snapshot, journal)

    def test_pilot_limits_and_omitted_zero_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = self.snapshot(folder)
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            full = self.journal(folder, "full.jsonl", digest, [("messages", "m1"), ("tool_results", "t1")])
            records = [json.loads(line) for line in full.read_text().splitlines()]
            for record in records:
                if record["stage"] == "applied":
                    record["stage"] = "skipped_changed_row"
            records[-1] = {"stage": "complete", "skipped": 2}
            self.write_jsonl(folder, "full.jsonl", records)
            for count in (0, 1, 2):
                for stage in ("batch_complete", "complete"):
                    pilot_records = records[:1 + 2 * count] + [{"stage": stage, **({"skipped": count} if count else {})}]
                    pilot = self.write_jsonl(folder, "pilot.jsonl", pilot_records)
                    valid = (stage == "complete") == (count == 2)
                    with self.subTest(count=count, stage=stage):
                        if valid:
                            result = reconcile_module.reconcile(snapshot, full, pilot)
                            self.assertEqual(result["applied"], 0)
                            self.assertEqual(result["skipped"], 2)
                            self.assertEqual(result["pilot_overlap_full"], count)
                        else:
                            with self.assertRaises(ValueError):
                                reconcile_module.reconcile(snapshot, full, pilot)
            pilot_records = records[:3] + [{"stage": "batch_complete", "skipped": 1}]
            pilot_records[0] = {**records[0], "planned": 1}
            pilot = self.write_jsonl(folder, "pilot.jsonl", pilot_records)
            with self.assertRaisesRegex(ValueError, "planned"):
                reconcile_module.reconcile(snapshot, full, pilot)

    def test_invalid_snapshot_structure(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = self.snapshot(folder)
            original = [json.loads(line) for line in snapshot.read_text().splitlines()]
            cases = [original[:2] + [original[1]] + original[2:], original[:-1],
                     [original[index] for index in (0, 1, 3, 2, 4, 5)], original + [original[-1]]]
            for index, key, value in ((0, "purpose", "wrong"), (2, "exported", 2),
                                      (2, "before", True), (1, "text", None), (1, "id", "")):
                records = copy.deepcopy(original)
                records[index][key] = value
                cases.append(records)
            for records in cases:
                with self.subTest(records=records):
                    self.write_jsonl(folder, "snapshot.jsonl", records)
                    with self.assertRaises(ValueError):
                        reconcile_module._validate_snapshot(snapshot)

    def test_read_is_bounded_and_json_is_strict(self):
        source = Mock()
        source.read.return_value = b"x" * 9
        opened = Mock()
        opened.__enter__ = Mock(return_value=source)
        opened.__exit__ = Mock(return_value=False)
        with patch.object(reconcile_module, "MAX_INPUT_BYTES", 8), patch.object(Path, "open", return_value=opened):
            with self.assertRaisesRegex(ValueError, "bound"):
                reconcile_module._read_jsonl("synthetic")
            source.read.assert_called_once_with(9)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "invalid.jsonl")
            for payload in (b'[]\n', b'{"kind":"header","kind":"footer"}\n', b'{"value":NaN}\n', b'\xff\n', b'\n', b'{'):
                path.write_bytes(payload)
                with self.assertRaises(ValueError):
                    reconcile_module._read_jsonl(path)

    def test_cli_failure_is_sanitized_and_success_is_aggregate_only(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = self.snapshot(folder)
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            journal = self.journal(folder, "private-path.jsonl", digest, [("messages", "m1"), ("tool_results", "t1")])
            command = [sys.executable, "-B", str(SCRIPTS / "reconcile_vector_quarantine.py"), str(snapshot), str(journal)]
            success = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(json.loads(success.stdout)["processed"], 2)
            self.assertNotIn("synthetic", success.stdout)
            journal.write_bytes(b'{"private-source":"SECRET-CONTENT"')
            failure = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(failure.returncode, 0)
            self.assertEqual(failure.stdout, "")
            self.assertEqual(failure.stderr, "Journal verification failed; evidence is invalid or incomplete.\n")

    def test_actual_exporter_and_operator_with_mock_connections(self):
        with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            modules = []
            for name in ("export_embedding_audit", "apply_vector_quarantine"):
                spec = importlib.util.spec_from_file_location("reconcile_fixture_" + name, SCRIPTS / (name + ".py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                modules.append(module)
        exporter, operator = modules
        vector = json.dumps([1.0] + [0.0] * 383)
        rows = [("synthetic-%02d" % number, "" if number == 0 else "synthetic source", vector) for number in range(11)]
        export_connection = Mock()
        export_cursor = export_connection.cursor.return_value
        export_cursor.fetchone.side_effect = [(10,), (10,), (1,), (1,)]
        export_cursor.fetchall.side_effect = [rows[:10], rows[10:]]
        output = io.StringIO()
        with patch.object(exporter.helios_writer, "_connect_raw", return_value=export_connection), contextlib.redirect_stdout(output):
            exporter.main()
        export_connection.close.assert_called_once()
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder, "snapshot.jsonl")
            snapshot.write_text(output.getvalue(), encoding="utf-8")
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            audit = Path(folder, "audit.json")
            audit.write_text(json.dumps({"input_sha256": digest, "stable_count_coverage": True,
                                        "tables": {"messages": {"known_hash_match": 10}, "tool_results": {"known_hash_match": 1}}}))
            rehearsal = Path(folder, "rehearsal.json")
            rehearsal.write_text(json.dumps({
                "snapshot_sha256": digest,
                "binary_sha256": "f929bfe46ef35f426e705ed01267b637cd73482d7fd722387e192679814f80d4",
                "restored": 11, "quarantined": 11, "roundtrip_hashes_match": True,
                "changed_source_guard": True, "idempotence": True, "changed_vector_guard": True,
                "nonempty_restore_guard": True,
            }))
            for name, limit, affected in (("pilot", 10, 1), ("full", 0, 0)):
                connection = Mock()
                connection.cursor.return_value.fetchone.return_value = ("HeliosDB Nano 3.58.1",)
                connection.cursor.return_value.rowcount = affected
                argv = ["apply", str(snapshot), str(audit), str(Path(folder, name)), "--expected-sha256", digest,
                        "--apply", "--rehearsal-report", str(rehearsal), "--limit", str(limit)]
                with patch.object(sys, "argv", argv), patch.object(operator.helios_writer, "_connect_raw", return_value=connection), \
                     patch.object(operator.helios_writer, "dsn", return_value="postgresql://synthetic:synthetic@172.17.0.1:5440/heliosdb"), \
                     patch.object(operator.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                    operator.main()
                connection.close.assert_called_once()
            result = reconcile_module.reconcile(snapshot, Path(folder, "full"), Path(folder, "pilot"))
            self.assertEqual((result["planned"], result["processed"], result["applied"], result["skipped"]), (11, 11, 0, 11))
            self.assertEqual((result["pilot_processed"], result["pilot_applied"], result["pilot_overlap_full"]), (10, 10, 10))


if __name__ == "__main__":
    unittest.main()
