import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from token_dashboard.request_limits import BoundedThreadingHTTPServer


class Request:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class BoundedThreadingHTTPServerTests(unittest.TestCase):
    def make_server(self, **kwargs):
        server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), Mock(), bind_and_activate=False, **kwargs
        )
        server.close_request = Mock()
        return server

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.005)
        self.fail("condition was not met")

    def test_max_two_workers_close_excess_connection_and_release_slot(self):
        server = self.make_server(max_workers=2)
        entered = threading.Barrier(2)
        release = threading.Event()
        handled = []

        def handle(request, address):
            handled.append(request)
            entered.wait()
            release.wait(2)

        server.process_request_thread = handle
        requests = [Request() for _ in range(3)]
        server.process_request(requests[0], ("first", 1))
        server.process_request(requests[1], ("second", 2))
        self.wait_for(lambda: len(handled) == 2)
        server.process_request(requests[2], ("third", 3))
        server.close_request.assert_called_once_with(requests[2])
        release.set()
        self.wait_for(lambda: server._worker_slots.acquire(blocking=False))
        server._worker_slots.release()
        server.server_close()

    def test_cleanup_runs_in_worker_and_slot_releases_after_handler_exception(self):
        cleanup_threads = []

        def cleanup():
            cleanup_threads.append(threading.current_thread())
            raise RuntimeError("cleanup failure")

        server = self.make_server(max_workers=1, cleanup=cleanup)
        worker_finished = threading.Event()

        def handle(request, address):
            worker_finished.set()
            raise RuntimeError("handler failure")

        server.process_request_thread = handle
        request = Request()
        with patch("threading.excepthook") as exception_hook:
            server.process_request(request, ("client", 1))
            self.assertTrue(worker_finished.wait(2))
            self.wait_for(lambda: cleanup_threads)
            exception_hook.assert_called_once()
        self.assertEqual(len(cleanup_threads), 1)
        self.assertNotEqual(cleanup_threads[0], threading.current_thread())
        self.assertTrue(server._worker_slots.acquire(blocking=False))
        server._worker_slots.release()
        server.server_close()

    def test_timeout_is_set_before_worker_handling(self):
        server = self.make_server()
        handled = threading.Event()
        server.process_request_thread = lambda request, address: handled.set()
        request = Request()
        server.process_request(request, ("client", 1))
        self.assertTrue(handled.wait(2))
        self.assertEqual(request.timeouts, [30.0])
        server.server_close()

    def test_thread_start_failure_releases_slot_and_closes_connection(self):
        server = self.make_server(max_workers=1)
        request = Request()
        worker = Mock()
        worker.start.side_effect = RuntimeError("cannot start")
        with patch("token_dashboard.request_limits.threading.Thread", return_value=worker):
            with self.assertRaises(RuntimeError):
                server.process_request(request, ("client", 1))
        self.assertTrue(server._worker_slots.acquire(blocking=False))
        server._worker_slots.release()
        server.close_request.assert_called_once_with(request)
        server.server_close()

    def test_excess_connection_is_closed_with_real_socketpair(self):
        server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), Mock(), bind_and_activate=False, max_workers=1
        )
        server._worker_slots.acquire()
        client_socket, server_socket = socket.socketpair()
        try:
            server.process_request(server_socket, ("client", 1))
            client_socket.settimeout(1)
            self.assertEqual(client_socket.recv(1), b"")
        finally:
            server._worker_slots.release()
            client_socket.close()
            server.server_close()

    def test_thread_construction_failure_releases_slot_and_closes_connection(self):
        server = self.make_server(max_workers=1)
        request = Request()
        with patch(
            "token_dashboard.request_limits.threading.Thread",
            side_effect=RuntimeError("cannot construct"),
        ):
            with self.assertRaises(RuntimeError):
                server.process_request(request, ("client", 1))
        self.assertTrue(server._worker_slots.acquire(blocking=False))
        server._worker_slots.release()
        server.close_request.assert_called_once_with(request)
        server.server_close()


if __name__ == "__main__":
    unittest.main()
