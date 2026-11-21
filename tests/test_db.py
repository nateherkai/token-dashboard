import os
import sqlite3
import tempfile
import unittest
from token_dashboard.db import init_db, connect


class InitDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")

    def test_init_creates_expected_tables(self):
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"files", "messages", "tool_calls", "plan", "dismissed_tips"}
        self.assertTrue(expected.issubset(tables), f"Missing: {expected - tables}")

    def test_init_is_idempotent(self):
        init_db(self.db_path)
        init_db(self.db_path)

    def test_tool_calls_delete_by_message_uuid_uses_index(self):
        # The scanner runs this delete once per message; a full table scan
        # here makes the first scan of a large history quadratic.
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            plan = " ".join(r[3] for r in c.execute(
                "EXPLAIN QUERY PLAN DELETE FROM tool_calls WHERE message_uuid=?", ("x",)))
        self.assertIn("idx_tools_message", plan)

    def test_connect_returns_row_factory(self):
        init_db(self.db_path)
        with connect(self.db_path) as c:
            r = c.execute("SELECT 1 AS one").fetchone()
        self.assertEqual(r["one"], 1)


if __name__ == "__main__":
    unittest.main()
