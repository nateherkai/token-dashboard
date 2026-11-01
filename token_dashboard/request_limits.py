"""Bounded worker HTTP server for local dashboard traffic."""
from __future__ import annotations

import http.server
import threading


class BoundedThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """A ThreadingHTTPServer with a fixed number of active request workers."""

    daemon_threads = True
    DEFAULT_MAX_WORKERS = 64
    DEFAULT_REQUEST_TIMEOUT = 30.0

    def __init__(
        self,
        server_address,
        request_handler_class,
        bind_and_activate=True,
        *,
        max_workers=DEFAULT_MAX_WORKERS,
        request_timeout=DEFAULT_REQUEST_TIMEOUT,
        cleanup=None,
        cleanup_callback=None,
    ):
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if request_timeout is not None and request_timeout <= 0:
            raise ValueError("request_timeout must be positive or None")
        if cleanup is not None and cleanup_callback is not None:
            raise ValueError("pass only one cleanup callback")
        super().__init__(server_address, request_handler_class, bind_and_activate)
        self.max_workers = max_workers
        self.request_timeout = request_timeout
        self.cleanup_callback = cleanup if cleanup is not None else cleanup_callback
        self._worker_slots = threading.BoundedSemaphore(max_workers)

    def process_request(self, request, client_address):
        if self.request_timeout is not None:
            request.settimeout(self.request_timeout)
        if not self._worker_slots.acquire(blocking=False):
            self.close_request(request)
            return

        try:
            worker = threading.Thread(
                target=self._run_request,
                args=(request, client_address),
            )
            worker.daemon = self.daemon_threads
            worker.start()
        except BaseException:
            self._worker_slots.release()
            self.close_request(request)
            raise

    def _run_request(self, request, client_address):
        try:
            self.process_request_thread(request, client_address)
        finally:
            try:
                if self.cleanup_callback is not None:
                    self.cleanup_callback()
            finally:
                self._worker_slots.release()
