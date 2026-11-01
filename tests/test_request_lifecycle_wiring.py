import io
import unittest
from unittest.mock import Mock, patch

from token_dashboard import helios_writer, server
from token_dashboard.scan_control import ScanBusy


class LifecycleWiringTests(unittest.TestCase):
    def test_busy_force_scan_rejects_before_scheduling_or_clearing_offsets(self):
        handler_class = server.build_handler("/unused", "/unused")
        handler = object.__new__(handler_class)
        handler.path = "/api/scan/force"
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        handler.wfile = Mock()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler._is_authenticated = lambda: True
        handler._csrf_valid = lambda: True
        with patch.object(server.SCAN_COORDINATOR, "start", side_effect=ScanBusy()), \
             patch.object(server, "_background_force_scan") as operation:
            handler.do_POST()
        handler.send_response.assert_called_once_with(409)
        operation.assert_not_called()

    def test_server_uses_worker_cleanup_and_closes_on_exit(self):
        httpd = Mock()
        httpd.serve_forever.side_effect = KeyboardInterrupt()
        with patch.object(server.threading, "Thread"), \
             patch.object(server, "BoundedThreadingHTTPServer", return_value=httpd) as factory:
            with self.assertRaises(KeyboardInterrupt):
                server.run("127.0.0.1", 0, "/unused", "/unused")
        self.assertIs(factory.call_args.kwargs["cleanup"], helios_writer.close_thread_connection)
        httpd.server_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
