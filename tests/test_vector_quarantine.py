import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from vector_quarantine import change_vector

SPEC = importlib.util.spec_from_file_location("apply_vector_quarantine", SCRIPTS / "apply_vector_quarantine.py")
apply_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply_module)


class VectorQuarantineTests(unittest.TestCase):
    def record(self):
        return {"table": "messages", "id": "synthetic-id", "text": "synthetic source", "vector": "[1.0]"}

    def test_quarantine_compares_identifier_source_and_original_vector(self):
        connection = Mock()
        connection.cursor.return_value.rowcount = 1
        record = self.record()
        self.assertEqual(change_vector(connection, record), 1)
        sql, params = connection.cursor.return_value.execute.call_args.args
        self.assertIn("uuid=%s AND prompt_text=%s AND CAST(body_vec AS TEXT)=%s", sql)
        self.assertEqual(params, (record["id"], record["text"], record["vector"]))
        connection.cursor.return_value.close.assert_called_once()

    def test_restore_only_targets_null_vector_with_unchanged_source(self):
        connection = Mock()
        connection.cursor.return_value.rowcount = 1
        record = self.record()
        self.assertEqual(change_vector(connection, record, restore=True), 1)
        sql, params = connection.cursor.return_value.execute.call_args.args
        self.assertIn("prompt_text=%s AND body_vec IS NULL", sql)
        self.assertEqual(params, (record["vector"], record["id"], record["text"]))

    def test_changed_row_skips_and_unexpected_count_rejects(self):
        connection = Mock()
        connection.cursor.return_value.rowcount = 0
        self.assertEqual(change_vector(connection, self.record()), 0)
        connection.cursor.return_value.rowcount = 2
        with self.assertRaises(RuntimeError):
            change_vector(connection, self.record())

    def test_unknown_table_or_missing_source_never_executes(self):
        connection = Mock()
        record = self.record()
        record["table"] = "unexpected"
        with self.assertRaises(KeyError):
            change_vector(connection, record)
        record = self.record()
        record["text"] = None
        with self.assertRaises(ValueError):
            change_vector(connection, record)
        connection.cursor.assert_not_called()

    def test_apply_requires_completed_rehearsal_before_database_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder, "snapshot.jsonl")
            snapshot.write_text("synthetic snapshot")
            digest = apply_module.file_digest(snapshot)
            report = Path(folder, "report.json")
            report.write_text(json.dumps({"input_sha256": digest, "stable_count_coverage": True, "tables": {
                "messages": {"known_hash_match": 1}, "tool_results": {"known_hash_match": 1},
            }}))
            argv = ["apply", str(snapshot), str(report), str(Path(folder, "journal")), "--expected-sha256", digest, "--apply"]
            with patch.object(sys, "argv", argv), patch.object(apply_module.helios_writer, "_connect_raw") as connect:
                with self.assertRaisesRegex(ValueError, "rehearsal evidence"):
                    apply_module.main()
                connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
