import http.server
import pathlib
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from token_dashboard import bulk_migrate, db, docling_ingest, embedder
from token_dashboard import helios_writer as writer
from token_dashboard import scanner, server


class EmbeddingConsumerTests(unittest.TestCase):
    def test_scanner_writes_null_vectors_when_provider_is_unavailable(self):
        connection = Mock()
        message = {
            "uuid": "test-message", "session_id": "test-session", "project_slug": "test-project",
            "type": "user", "timestamp": "2026-09-21T00:00:00Z", "prompt_text": "long prompt " * 10,
        }
        result = {
            "tool_name": "_tool_result", "tool_use_id": "test-tool", "message_uuid": message["uuid"],
            "session_id": message["session_id"], "project_slug": message["project_slug"],
            "timestamp": message["timestamp"], "result_tokens": 600, "body_text": "result " * 100,
        }
        with patch.object(writer, "is_configured", return_value=True), \
             patch.object(writer, "get_conn", return_value=connection), \
             patch.object(embedder.mcp_client, "is_configured", return_value=False):
            sink = scanner._build_helios_sink()
            sink(message, [result])
        writes = connection.cursor.return_value.execute.call_args_list
        writes = [call for call in writes if "INSERT INTO dashboard." in call.args[0]]
        self.assertEqual(len(writes), 2)
        self.assertIsNone(writes[0].args[1][-1])
        self.assertIsNone(writes[1].args[1][-1])

    def test_document_ingest_keeps_text_but_writes_no_synthetic_vector(self):
        connection = Mock()
        with tempfile.TemporaryDirectory() as folder:
            pathlib.Path(folder, "README.md").write_text("# Test document\n\n" + "Document body. " * 10)
            with patch.object(writer, "get_conn", return_value=connection), \
                 patch.object(embedder.mcp_client, "is_configured", return_value=False):
                outcome = docling_ingest.ingest_project("test-project", cwd=folder)
        self.assertTrue(outcome["ok"])
        writes = connection.cursor.return_value.execute.call_args_list
        message_writes = [call for call in writes if "INSERT INTO dashboard.messages" in call.args[0]]
        self.assertTrue(message_writes)
        for call in message_writes:
            self.assertIsNone(call.args[1][-1])
            self.assertIn("Document body", call.args[1][-3])

    def test_bulk_migration_does_not_count_missing_vectors_as_embedded(self):
        connection = Mock()
        with tempfile.TemporaryDirectory() as folder:
            database = str(pathlib.Path(folder, "test.db"))
            db.init_db(database)
            with sqlite3.connect(database) as local:
                local.execute(
                    "INSERT INTO messages (uuid,session_id,project_slug,type,timestamp,prompt_text) VALUES (?,?,?,?,?,?)",
                    ("test", "session", "project", "user", "2026-09-21", "Prompt text " * 10),
                )
            with patch.object(writer, "is_configured", return_value=True), \
                 patch.object(writer, "get_conn", return_value=connection), \
                 patch.object(embedder.mcp_client, "is_configured", return_value=False):
                outcome = bulk_migrate.migrate(database)
        self.assertEqual(outcome["messages_pushed"], 1)
        self.assertEqual(outcome["messages_embedded"], 0)
        writes = connection.cursor.return_value.execute.call_args_list
        self.assertIsNone(writes[0].args[1][-1])

    def test_search_returns_503_without_database_query_when_embedding_missing(self):
        with patch.object(server.auth_mod, "is_enabled", return_value=False), \
             patch.object(embedder.mcp_client, "is_configured", return_value=False), \
             patch.object(writer, "search_prompts") as search:
            handler = server.build_handler("/unused", "/unused")
            httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/search?q=test-query" % httpd.server_port, timeout=5,
                    )
                with caught.exception as response:
                    self.assertEqual(response.code, 503)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                search.assert_not_called()
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
