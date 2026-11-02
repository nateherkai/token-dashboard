import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location(
    "apply_vector_quarantine_preflight", SCRIPTS / "apply_vector_quarantine.py"
)
apply_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply_module)


class VectorQuarantinePreflightTests(unittest.TestCase):
    def vector(self, first=1.0):
        return json.dumps([first] + [0.0] * 383)

    def snapshot_payload(self, rows=None, summaries=None, footer=None):
        if rows is None:
            rows = [
                {"kind": "row", "table": "messages", "id": "message-1", "text": "synthetic message", "vector": self.vector()},
                {"kind": "row", "table": "tool_results", "id": "tool-1", "text": "synthetic tool result", "vector": self.vector(2.0)},
            ]
        if summaries is None:
            summaries = {
                "messages": 1,
                "tool_results": 1,
            }
        records = [{"kind": "header", "format": "vector-quarantine-v1"}, *rows]
        for table, count in summaries.items():
            records.append({"kind": "table_summary", "table": table, "before": count, "exported": count, "after": count})
        records.append({"kind": "footer", "complete": True} if footer is None else footer)
        return ("\n".join(json.dumps(record) for record in records) + "\n").encode()

    def make_files(self, folder, payload=None, report=None, rehearsal=None):
        snapshot = Path(folder, "snapshot.jsonl")
        if payload is None:
            payload = self.snapshot_payload()
        snapshot.write_bytes(payload)
        digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        if report is None:
            report = {
                "input_sha256": digest,
                "stable_count_coverage": True,
                "tables": {
                    "messages": {"known_hash_match": 1},
                    "tool_results": {"known_hash_match": 1},
                },
            }
        report_path = Path(folder, "report.json")
        report_path.write_text(json.dumps(report), encoding="utf-8")
        rehearsal_path = None
        if rehearsal is not None:
            rehearsal_path = Path(folder, "rehearsal.json")
            rehearsal_path.write_text(json.dumps(rehearsal), encoding="utf-8")
        return snapshot, report_path, rehearsal_path, digest

    def run_apply(self, snapshot, report, journal, digest, rehearsal=None):
        argv = [
            "apply",
            str(snapshot),
            str(report),
            str(journal),
            "--expected-sha256",
            digest,
            "--apply",
        ]
        if rehearsal is not None:
            argv.extend(["--rehearsal-report", str(rehearsal)])
        with patch.object(sys, "argv", argv):
            apply_module.main()

    def valid_rehearsal(self, digest, expected=2):
        return {
            "snapshot_sha256": digest,
            "binary_sha256": (
                "f929bfe46ef35f426e705ed01267b637cd73482d7fd722387e192679814f80d4"
            ),
            "restored": expected,
            "quarantined": expected,
            "roundtrip_hashes_match": True,
            "changed_source_guard": True,
            "idempotence": True,
            "changed_vector_guard": True,
            "nonempty_restore_guard": True,
        }

    def test_malformed_or_mismatched_rehearsal_report_is_rejected_before_connection(self):
        for malformed in (True, False):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as folder:
                snapshot, report, rehearsal, digest = self.make_files(
                    folder, rehearsal=self.valid_rehearsal(digest="placeholder")
                )
                rehearsal.write_text(
                    "{" if malformed else json.dumps(self.valid_rehearsal("0" * len(digest))),
                    encoding="utf-8",
                )
                with patch.object(apply_module.helios_writer, "_connect_raw") as connect:
                    with self.assertRaises((ValueError, json.JSONDecodeError)):
                        self.run_apply(snapshot, report, Path(folder, "journal"), digest, rehearsal)
                    connect.assert_not_called()

    def test_wrong_snapshot_hash_is_rejected_before_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot, report, _, digest = self.make_files(folder)
            with patch.object(apply_module.helios_writer, "_connect_raw") as connect:
                with self.assertRaisesRegex(ValueError, "Snapshot hash mismatch"):
                    self.run_apply(
                        snapshot,
                        report,
                        Path(folder, "journal"),
                        "0" * len(digest),
                    )
                connect.assert_not_called()

    def test_unknown_classification_is_rejected_before_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            report = {
                "input_sha256": "unused",
                "stable_count_coverage": True,
                "tables": {
                    "messages": {"known_hash_match": 1, "unknown": 1},
                    "tool_results": {"known_hash_match": 1},
                },
            }
            snapshot, report_path, _, digest = self.make_files(folder, report=report)
            report_path.write_text(
                json.dumps({**report, "input_sha256": digest}), encoding="utf-8"
            )
            with patch.object(apply_module.helios_writer, "_connect_raw") as connect:
                with self.assertRaisesRegex(ValueError, "legacy hash vectors"):
                    self.run_apply(snapshot, report_path, Path(folder, "journal"), digest)
                connect.assert_not_called()

    def test_malformed_snapshot_is_rejected_before_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = self.snapshot_payload() + b'{"kind":"row"\n'
            snapshot, report, rehearsal, digest = self.make_files(
                folder, payload=payload, rehearsal=self.valid_rehearsal("placeholder")
            )
            rehearsal.write_text(json.dumps(self.valid_rehearsal(digest)), encoding="utf-8")
            with patch.object(apply_module.helios_writer, "_connect_raw") as connect:
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    self.run_apply(snapshot, report, Path(folder, "journal"), digest, rehearsal)
                connect.assert_not_called()

    def test_snapshot_count_mismatch_is_rejected_before_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            report = {
                "input_sha256": "unused",
                "stable_count_coverage": True,
                "tables": {
                    "messages": {"known_hash_match": 2},
                    "tool_results": {"known_hash_match": 1},
                },
            }
            snapshot, report_path, rehearsal, digest = self.make_files(
                folder, report=report, rehearsal=self.valid_rehearsal("placeholder", expected=3)
            )
            report_path.write_text(json.dumps({**report, "input_sha256": digest}), encoding="utf-8")
            rehearsal.write_text(json.dumps(self.valid_rehearsal(digest, expected=3)), encoding="utf-8")
            with patch.object(
                apply_module.helios_writer,
                "dsn",
                return_value="postgresql://user:pw@172.17.0.1:5440/heliosdb",
            ), patch.object(apply_module.helios_writer, "_connect_raw") as connect:
                with self.assertRaisesRegex(ValueError, "row counts"):
                    self.run_apply(snapshot, report_path, Path(folder, "journal"), digest, rehearsal)
                connect.assert_not_called()

    def test_duplicate_snapshot_identifier_is_rejected_before_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [
                {"kind": "row", "table": "messages", "id": "duplicate", "text": "first", "vector": self.vector()},
                {"kind": "row", "table": "messages", "id": "duplicate", "text": "second", "vector": self.vector(2.0)},
                {"kind": "row", "table": "tool_results", "id": "tool-1", "text": "synthetic tool result", "vector": self.vector(3.0)},
            ]
            report = {
                "input_sha256": "unused",
                "stable_count_coverage": True,
                "tables": {
                    "messages": {"known_hash_match": 2},
                    "tool_results": {"known_hash_match": 1},
                },
            }
            snapshot, report_path, rehearsal, digest = self.make_files(
                folder,
                payload=self.snapshot_payload(rows=rows, summaries={"messages": 2, "tool_results": 1}),
                report=report,
                rehearsal=self.valid_rehearsal("placeholder", expected=3),
            )
            report_path.write_text(json.dumps({**report, "input_sha256": digest}), encoding="utf-8")
            rehearsal.write_text(json.dumps(self.valid_rehearsal(digest, expected=3)), encoding="utf-8")
            with patch.object(
                apply_module.helios_writer,
                "dsn",
                return_value="postgresql://user:pw@172.17.0.1:5440/heliosdb",
            ), patch.object(apply_module.helios_writer, "_connect_raw") as connect:
                with self.assertRaisesRegex(ValueError, "Duplicate identifier"):
                    self.run_apply(snapshot, report_path, Path(folder, "journal"), digest, rehearsal)
                connect.assert_not_called()

    def test_invalid_snapshot_vector_is_rejected_before_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [
                {"kind": "row", "table": "messages", "id": "message-1", "text": "synthetic message", "vector": self.vector(first=0.0)},
                {"kind": "row", "table": "tool_results", "id": "tool-1", "text": "synthetic tool result", "vector": self.vector(2.0)},
            ]
            snapshot, report, rehearsal, digest = self.make_files(
                folder, payload=self.snapshot_payload(rows=rows), rehearsal=self.valid_rehearsal("placeholder")
            )
            rehearsal.write_text(json.dumps(self.valid_rehearsal(digest)), encoding="utf-8")
            with patch.object(
                apply_module.helios_writer,
                "dsn",
                return_value="postgresql://user:pw@172.17.0.1:5440/heliosdb",
            ), patch.object(apply_module.helios_writer, "_connect_raw") as connect:
                with self.assertRaisesRegex(ValueError, "invalid snapshot vector"):
                    self.run_apply(snapshot, report, Path(folder, "journal"), digest, rehearsal)
                connect.assert_not_called()

    def test_frozen_records_do_not_change_after_source_payload_mutation(self):
        payload = bytearray(self.snapshot_payload())
        frozen = tuple(apply_module.frozen_records(payload))
        payload[:] = b"{}\n"
        self.assertEqual([record["id"] for record in frozen], ["message-1", "tool-1"])

    def test_apply_uses_frozen_backup_if_original_file_changes_after_preflight(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot, report, rehearsal, digest = self.make_files(
                folder, rehearsal=self.valid_rehearsal("placeholder")
            )
            rehearsal.write_text(json.dumps(self.valid_rehearsal(digest)), encoding="utf-8")
            journal = Path(folder, "journal")
            connection = Mock()
            connection.cursor.return_value.fetchone.return_value = ["PostgreSQL 16.0 (HeliosDB Nano 3.58.1)"]
            connection.cursor.return_value.rowcount = 1

            def connect_after_source_change():
                snapshot.write_text("changed after preflight", encoding="utf-8")
                return connection

            with patch.object(apply_module.helios_writer, "dsn", return_value="postgresql://user:pw@172.17.0.1:5440/heliosdb"), \
                 patch.object(apply_module.helios_writer, "_connect_raw", side_effect=connect_after_source_change), \
                 patch.object(apply_module.time, "sleep"), patch("builtins.print"):
                self.run_apply(snapshot, report, journal, digest, rehearsal)
            updates = [call for call in connection.cursor.return_value.execute.call_args_list if call.args[0].startswith("UPDATE")]
            self.assertEqual({call.args[1][0] for call in updates}, {"message-1", "tool-1"})
            frozen_backup = Path(str(journal) + ".snapshot.jsonl")
            self.assertEqual(hashlib.sha256(frozen_backup.read_bytes()).hexdigest(), digest)
            self.assertEqual(frozen_backup.stat().st_mode & 0o777, 0o400)
            self.assertEqual(json.loads(journal.read_text().splitlines()[-1])["changed"], 2)

    def test_unsafe_target_is_rejected_before_journal_and_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot, report, rehearsal, digest = self.make_files(
                folder, rehearsal=self.valid_rehearsal("placeholder")
            )
            rehearsal.write_text(json.dumps(self.valid_rehearsal(digest)), encoding="utf-8")
            journal = Path(folder, "journal")
            with patch.object(apply_module.helios_writer, "dsn", return_value="postgresql://pw@127.0.0.1:5440/heliosdb"), patch.object(
                apply_module.helios_writer, "_connect_raw"
            ) as connect:
                with self.assertRaisesRegex(ValueError, "Configured target"):
                    self.run_apply(snapshot, report, journal, digest, rehearsal)
                connect.assert_not_called()
            self.assertFalse(journal.exists())

    def test_existing_journal_cannot_be_overwritten_before_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot, report, rehearsal, digest = self.make_files(
                folder, rehearsal=self.valid_rehearsal("placeholder")
            )
            rehearsal.write_text(json.dumps(self.valid_rehearsal(digest)), encoding="utf-8")
            journal = Path(folder, "journal")
            journal.write_text("pre-existing synthetic journal\n", encoding="utf-8")
            with patch.object(
                apply_module.helios_writer,
                "dsn",
                return_value="postgresql://user:pw@172.17.0.1:5440/heliosdb",
            ), patch.object(apply_module.helios_writer, "_connect_raw") as connect:
                with self.assertRaises(FileExistsError):
                    self.run_apply(snapshot, report, journal, digest, rehearsal)
                connect.assert_not_called()
            self.assertEqual(journal.read_text(encoding="utf-8"), "pre-existing synthetic journal\n")


if __name__ == "__main__":
    unittest.main()
