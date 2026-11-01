import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "verify_quarantine_sources", SCRIPTS / "verify_quarantine_sources.py"
)
verify_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_module)


class VerifyQuarantineSourcesTests(unittest.TestCase):
    def write_snapshot(self, folder, rows):
        counts = {table: sum(row["table"] == table for row in rows) for table in verify_module.TABLE_COLUMNS}
        records = [
            {"kind": "header", "purpose": "read-only bounded vector audit"},
            *[row for row in rows if row["table"] == "messages"],
        ]
        records.append({"kind": "table_summary", "table": "messages", "before": counts["messages"],
                        "exported": counts["messages"], "after": counts["messages"]})
        records.extend(row for row in rows if row["table"] == "tool_results")
        records.append({"kind": "table_summary", "table": "tool_results", "before": counts["tool_results"],
                        "exported": counts["tool_results"], "after": counts["tool_results"]})
        records.append({"kind": "footer", "complete": True})
        path = Path(folder, "snapshot.jsonl")
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def row(self, table, identifier, text, vector):
        return {"kind": "row", "table": table, "id": identifier, "text": text, "vector": vector}

    def connection(self, returned_rows):
        connection = Mock()
        cursor = connection.cursor.return_value
        cursor.fetchall.side_effect = returned_rows
        return connection, cursor

    def test_classifies_present_missing_source_and_vector_without_sensitive_output(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [
                self.row("messages", "m1", "original message", "[1, 0]"),
                self.row("messages", "m2", "another message", "[2, 0]"),
                self.row("messages", "m3", "missing message", "[4, 0]"),
            ]
            rows.append(self.row("tool_results", "t1", "original result", "[3, 0]"))
            snapshot, digest = self.write_snapshot(folder, rows)
            connection, cursor = self.connection([
                [("m1", "original message", None), ("m2", "changed message", "[2, 0]")],
                [("t1", "original result", "[9, 0]")],
            ])

            report = verify_module.verify(connection, snapshot, digest)

            self.assertEqual(report, {
                "snapshot_sha256": digest,
                "snapshot_rows": 4,
                "present": 3,
                "missing": 1,
                "unchanged_source": 2,
                "changed_source": 1,
                "current_null": 1,
                "current_non_null": 2,
                "original_vector_still_present": 1,
                "query_count": 2,
            })
            self.assertNotIn("m1", json.dumps(report))
            self.assertNotIn("original message", json.dumps(report))
            self.assertNotIn("[1, 0]", json.dumps(report))
            cursor.close.assert_called_once_with()

    def test_queries_are_select_only_parameterized_and_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [self.row("messages", "m1", "secret text", "secret vector")]
            snapshot, digest = self.write_snapshot(folder, rows)
            connection, cursor = self.connection([[('m1', 'secret text', None)]])

            verify_module.verify(connection, snapshot, digest)

            query, parameters = cursor.execute.call_args.args
            self.assertTrue(query.startswith("SELECT "))
            self.assertIn("FROM dashboard.messages", query)
            self.assertIn("CAST(body_vec AS TEXT)", query)
            self.assertIn("IN (%s)", query)
            self.assertIn("LIMIT 2", query)
            self.assertNotIn("secret", query)
            self.assertEqual(parameters, ("m1",))
            self.assertNotIn("UPDATE", query.upper())
            self.assertNotIn("INSERT", query.upper())
            self.assertNotIn("DELETE", query.upper())

    def test_snapshot_hash_and_format_are_validated_before_any_query(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot, digest = self.write_snapshot(folder, [self.row("messages", "m1", "text", "[1]")])
            connection = Mock()
            with self.assertRaisesRegex(ValueError, "hash"):
                verify_module.verify(connection, snapshot, "0" * 64)
            connection.cursor.assert_not_called()

            snapshot.write_text(snapshot.read_text(encoding="utf-8") + "{\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_module.verify(connection, snapshot, digest)
            connection.cursor.assert_not_called()

    def test_rejects_malformed_duplicate_and_unexpected_database_rows(self):
        cases = [
            [[("m1", "text")]],
            [[("m1", "text", None), ("m1", "text", None)]],
            [[("unexpected", "text", None)]],
            [[("m1", 3, None)]],
            [[("m1", "text", object())]],
        ]
        for returned in cases:
            with self.subTest(returned=returned):
                with tempfile.TemporaryDirectory() as folder:
                    snapshot, digest = self.write_snapshot(
                        folder, [self.row("messages", "m1", "text", "[1]")]
                    )
                    connection, cursor = self.connection(returned)
                    with self.assertRaises(ValueError):
                        verify_module.verify(connection, snapshot, digest)
                    cursor.close.assert_called_once_with()

    def test_batches_each_table_at_most_one_hundred_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [self.row("messages", "m%03d" % index, "text", "[1]") for index in range(101)]
            snapshot, digest = self.write_snapshot(folder, rows)
            connection = Mock()
            cursor = connection.cursor.return_value
            cursor.fetchall.side_effect = [
                [("m%03d" % index, "text", None) for index in range(100)],
                [("m100", "text", None)],
            ]

            report = verify_module.verify(connection, snapshot, digest)

            self.assertEqual(report["query_count"], 2)
            self.assertEqual(report["present"], 101)
            self.assertEqual(len(cursor.execute.call_args_list), 2)
            for call in cursor.execute.call_args_list:
                self.assertLessEqual(len(call.args[1]), 100)
                self.assertNotIn("m", call.args[0].split("IN (", 1)[1])

    def test_rejects_changed_second_snapshot_read(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot, digest = self.write_snapshot(folder, [self.row("messages", "m1", "text", "[1]")])
            connection = Mock()
            original_read = verify_module._read_jsonl
            calls = [0]

            def read_twice(path):
                calls[0] += 1
                payload, records = original_read(path)
                if calls[0] == 1:
                    payload += b"\n"
                return payload, records

            verify_module._read_jsonl = read_twice
            try:
                with self.assertRaisesRegex(ValueError, "changed"):
                    verify_module.verify(connection, snapshot, digest)
            finally:
                verify_module._read_jsonl = original_read
            connection.cursor.assert_not_called()

    def test_rejects_result_payload_over_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot, digest = self.write_snapshot(folder, [self.row("messages", "m1", "text", "[1]")])
            connection, cursor = self.connection([[('m1', 'source', 'vector')]])
            original_limit = verify_module.MAX_RESULT_BYTES
            verify_module.MAX_RESULT_BYTES = 5
            try:
                with self.assertRaisesRegex(ValueError, "payload bound"):
                    verify_module.verify(connection, snapshot, digest)
            finally:
                verify_module.MAX_RESULT_BYTES = original_limit
            cursor.close.assert_called_once_with()

    def test_rejects_snapshot_row_count_over_bound_before_query(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot, digest = self.write_snapshot(folder, [self.row("messages", "m1", "text", "[1]")])
            connection = Mock()
            original_limit = verify_module.MAX_SNAPSHOT_ROWS
            verify_module.MAX_SNAPSHOT_ROWS = 0
            try:
                with self.assertRaisesRegex(ValueError, "row bound"):
                    verify_module.verify(connection, snapshot, digest)
            finally:
                verify_module.MAX_SNAPSHOT_ROWS = original_limit
            connection.cursor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
