import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("run_embedding_audit", SCRIPTS / "run_embedding_audit.py")
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)


class AuditRunnerTests(unittest.TestCase):
    def records(self):
        records = [{"kind": "header"}]
        for table in ["messages", "tool_results"]:
            records.append({"kind": "row", "table": table, "id": "test", "text": "private synthetic text", "vector": [1.0] * 384})
            records.append({"kind": "table_summary", "table": table, "before": 1, "exported": 1, "after": 1})
        records.append({"kind": "footer", "complete": True})
        return records

    def run_audit(self, records):
        with tempfile.TemporaryDirectory() as folder:
            input_path = Path(folder, "input.jsonl")
            input_path.write_text("".join(json.dumps(record) + "\n" for record in records))
            with patch.object(audit_module, "load_legacy_function", return_value=(lambda text: [1.0] * 384, "source-hash")):
                return audit_module.audit(input_path, "unused")

    def test_complete_counts_and_no_raw_content_in_report(self):
        report = self.run_audit(self.records())
        self.assertTrue(report["stable_count_coverage"])
        self.assertEqual(report["tables"]["messages"]["known_hash_match"], 1)
        self.assertNotIn("private synthetic text", json.dumps(report))

    def test_missing_footer_aborts(self):
        with self.assertRaises(ValueError):
            self.run_audit(self.records()[:-1])

    def test_duplicate_identifier_aborts(self):
        records = self.records()
        records.insert(2, records[1])
        with self.assertRaises(ValueError):
            self.run_audit(records)

    def test_changed_counts_do_not_claim_stability(self):
        records = self.records()
        records[2]["after"] = 2
        self.assertFalse(self.run_audit(records)["stable_count_coverage"])


if __name__ == "__main__":
    unittest.main()
