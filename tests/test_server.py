import http.server
import json
import os
import re
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import urllib.request

from token_dashboard.db import init_db
from token_dashboard import auth as auth_mod
from token_dashboard import server as server_mod
from token_dashboard.server import (
    _SSEConnectionLimiter, _parse_float, _parse_int, _safe_next, build_handler,
)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens, prompt_text, prompt_chars) VALUES ('u',NULL,'s','p','user','2026-04-19T00:00:00Z',NULL,0,0,0,0,0,'hi',2)")
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens) VALUES ('a','u','s','p','assistant','2026-04-19T00:00:01Z','claude-haiku-4-5',1,1,0,0,0)")
            c.commit()
        self.port = _free_port()
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def test_index_html(self):
        body = self._get("/")
        self.assertIn(b"Token Dashboard", body)

    def test_overview_json(self):
        body = json.loads(self._get("/api/overview"))
        self.assertIn("sessions", body)
        self.assertEqual(body["sessions"], 1)

    def test_prompts_json(self):
        body = json.loads(self._get("/api/prompts?limit=10"))
        self.assertIsInstance(body, list)

    def test_projects_json(self):
        body = json.loads(self._get("/api/projects"))
        self.assertIsInstance(body, list)
        self.assertEqual(body[0]["project_slug"], "p")

    def test_plan_json(self):
        body = json.loads(self._get("/api/plan"))
        self.assertIn("plan", body)
        self.assertIn("pricing", body)

    def test_head_returns_200_not_501(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_head_api_endpoint(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/overview", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_safe_next_allows_local_path_and_rejects_external_target(self):
        self.assertEqual(_safe_next("/overview?since=2026-01"), "/overview?since=2026-01")
        self.assertEqual(_safe_next("https://example.invalid/next"), "/")
        self.assertEqual(_safe_next("//example.invalid/next"), "/")
        self.assertEqual(_safe_next(None), "/")
        self.assertEqual(_safe_next("/overview\x1f"), "/")
        self.assertEqual(_safe_next("/overview\x7f"), "/")

    def test_security_headers_on_normal_response(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as response:
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_invalid_query_numbers_return_400_without_backend_calls(self):
        cases = [
            ("/api/duplicate-retrievals?threshold=not-a-number", "helios.find_duplicate_retrievals"),
            ("/api/clusters/not-a-number", "clusters_mod.cluster_members"),
            ("/api/audit/verify?call_id=not-a-number", "audit_mod.verify"),
        ]
        for path, target in cases:
            with self.subTest(path=path), patch("token_dashboard.server." + target) as backend:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._get(path)
                self.assertEqual(caught.exception.code, 400)
                self.assertEqual(caught.exception.headers["X-Frame-Options"], "DENY")
                caught.exception.close()
                backend.assert_not_called()

    def test_invalid_post_numbers_return_400_without_backend_calls(self):
        cases = [
            ("/api/mcp/replay", {"call_id": "not-a-number"}, "mcp_client.replay_call"),
            ("/api/clusters/build", {"k": "not-a-number"}, "clusters_mod.cluster_prompts"),
            ("/api/alerts/ack", {"id": "not-a-number"}, "alerts_mod.acknowledge"),
            ("/api/helios/bulk-migrate", {"limit": "not-a-number"}, "bulk_migrate.migrate"),
        ]
        for path, body, target in cases:
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.port}" + path,
                data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
            )
            with self.subTest(path=path), patch("token_dashboard.server." + target) as backend:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code, 400)
                caught.exception.close()
                backend.assert_not_called()


class AuthenticatedServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        self.env = patch.dict(os.environ, {
            "TD_AUTH_USER": "admin",
            "TD_AUTH_PASSWORD": "secret",
            "TD_AUTH_SECRET": "test-secret-for-csrf",
        })
        self.env.start()
        self.port = _free_port()
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        self.opener = urllib.request.build_opener(NoRedirect)
        self.cookie_header = ""

    def tearDown(self):
        self.httpd.shutdown()
        self.env.stop()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _open_error(self, request):
        if self.cookie_header and not request.has_header("Cookie"):
            request.add_header("Cookie", self.cookie_header)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.opener.open(request)
        caught.exception.close()
        return caught.exception

    def _login(self):
        with self.opener.open(self._url("/login")) as response:
            page = response.read().decode()
            self.cookie_header = response.headers["Set-Cookie"].split(";", 1)[0]
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        form = urllib.parse.urlencode({
            "username": "admin", "password": "secret", "next": "/", "csrf_token": token,
        }).encode()
        request = urllib.request.Request(
            self._url("/login"), data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": self.cookie_header},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.opener.open(request)
        self.assertEqual(caught.exception.code, 302)
        self.cookie_header += "; " + caught.exception.headers["Set-Cookie"].split(";", 1)[0]
        caught.exception.close()

    def test_login_requires_signed_form_token_and_bootstraps_session_token(self):
        with self.opener.open(self._url("/login")) as response:
            page = response.read().decode()
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        form = urllib.parse.urlencode({"username": "admin", "password": "secret"}).encode()
        request = urllib.request.Request(
            self._url("/login"), data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": self.cookie_header},
        )
        error = self._open_error(request)
        self.assertEqual(error.code, 403)
        self._login()
        with self.opener.open(urllib.request.Request(self._url("/api/csrf"), headers={"Cookie": self.cookie_header})) as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            csrf = json.loads(response.read())["csrf_token"]
        self.assertTrue(csrf)
        self.assertNotIn("td_session", page)

    def test_authenticated_mutation_requires_session_bound_header(self):
        self._login()
        request = urllib.request.Request(
            self._url("/api/plan"), data=b'{"plan":"api"}',
            headers={"Content-Type": "application/json"},
        )
        error = self._open_error(request)
        self.assertEqual(error.code, 403)
        with self.opener.open(urllib.request.Request(self._url("/api/csrf"), headers={"Cookie": self.cookie_header})) as response:
            csrf = json.loads(response.read())["csrf_token"]
        self.assertFalse(auth_mod.verify_csrf(
            self.cookie_header.split("td_session=", 1)[1].split(";", 1)[0], "☃"))
        request.add_header("X-TD-CSRF", csrf)
        with patch("token_dashboard.server.set_plan") as set_plan:
            with self.opener.open(request) as response:
                self.assertEqual(response.status, 200)
            set_plan.assert_called_once()

    def test_legacy_session_cookie_remains_valid_with_derived_csrf(self):
        with patch.dict(os.environ, {"TD_AUTH_USER": "admin"}):
            payload = "admin|4102444800"
            encoded = auth_mod.base64.urlsafe_b64encode(payload.encode()).decode()
            cookie = f"{encoded}.{auth_mod._sign(payload)}"
            self.assertEqual(auth_mod.verify_cookie(cookie), "admin")
            self.assertTrue(auth_mod.verify_csrf(cookie, auth_mod.csrf_token(cookie)))
            self.assertIsNone(auth_mod.verify_cookie(f"{encoded}.☃"))
            self.assertFalse(auth_mod.verify_csrf(cookie, "☃"))
            self.assertFalse(auth_mod.verify_login_csrf(cookie, "☃"))

    def test_state_changing_get_routes_are_not_mutations(self):
        self._login()
        for path in ("/api/scan", "/api/scan/force", "/api/savings/backfill",
                     "/api/helios/refresh-mvs", "/api/helios/bulk-migrate"):
            with self.subTest(path=path):
                request = urllib.request.Request(self._url(path))
                error = self._open_error(request)
                self.assertEqual(error.code, 405)

    def test_stream_ends_at_bounded_lifetime(self):
        self._login()
        with patch.multiple(
            "token_dashboard.server",
            SSE_MAX_CONNECTIONS=1,
            SSE_MAX_CONNECTIONS_PER_SESSION=1,
            SSE_MAX_LIFETIME_SECONDS=0.01,
        ):
            handler = build_handler(self.db, projects_dir="/nonexistent")
            stream_server = http.server.HTTPServer(("127.0.0.1", _free_port()), handler)
            threading.Thread(target=stream_server.serve_forever, daemon=True).start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{stream_server.server_port}/api/stream",
                    headers={"Cookie": self.cookie_header},
                )
                with urllib.request.urlopen(request, timeout=1) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Connection"], "close")
                    response.read()
                threading.Event().wait(0.05)
                with urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{stream_server.server_port}/api/stream",
                        headers={"Cookie": self.cookie_header},
                    )
                ) as response:
                    self.assertEqual(response.status, 200)
            finally:
                stream_server.shutdown()
                stream_server.server_close()

class NumericParserTests(unittest.TestCase):
    def test_numeric_parsers_reject_malformed_nonfinite_and_out_of_bounds_values(self):
        self.assertEqual(_parse_int("12", "call_id", minimum=1), 12)
        self.assertEqual(_parse_float("0.5", "threshold", 0.0, 2.0), 0.5)
        for value in ("abc", "1.5", 1.5, True, None):
            with self.assertRaises(ValueError):
                _parse_int(value, "call_id", minimum=1)
        for value in ("nan", "inf", "-inf", "abc", True):
            with self.assertRaises(ValueError):
                _parse_float(value, "timeout", 0.1, 120.0)
        with self.assertRaises(ValueError):
            _parse_float("2.1", "threshold", 0.0, 2.0)


class SSELimiterTests(unittest.TestCase):
    def make_handler(self, limiter):
        with patch.object(server_mod, "_SSEConnectionLimiter", return_value=limiter):
            handler_class = build_handler("/unused", "/unused")
        handler = object.__new__(handler_class)
        handler.path = "/api/stream"
        handler.headers = {}
        handler.connection = Mock()
        handler.wfile = Mock()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler._is_authenticated = lambda: True
        handler._stream_identity = lambda: "synthetic-session"
        handler.close_connection = False
        return handler

    def test_handler_releases_on_write_failures_and_closes_connection(self):
        for failure in [BrokenPipeError(), ConnectionResetError(), TimeoutError()]:
            with self.subTest(failure=type(failure).__name__):
                limiter = _SSEConnectionLimiter(1, 1)
                handler = self.make_handler(limiter)
                handler.wfile.write.side_effect = failure
                with patch.object(server_mod.EVENTS, "get", return_value={"type": "test"}):
                    handler.do_GET()
                self.assertEqual(limiter.active, 0)
                self.assertTrue(handler.close_connection)
                self.assertTrue(limiter.acquire("synthetic-session"))
                self.assertTrue(all(0 < call.args[0] <= 5 for call in handler.connection.settimeout.call_args_list))

    def test_handler_releases_on_unexpected_error(self):
        limiter = _SSEConnectionLimiter(1, 1)
        handler = self.make_handler(limiter)
        with patch.object(server_mod.EVENTS, "get", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                handler.do_GET()
        self.assertEqual(limiter.active, 0)
        self.assertTrue(handler.close_connection)

    def test_handler_rechecks_deadline_after_queue_wait(self):
        limiter = _SSEConnectionLimiter(1, 1)
        handler = self.make_handler(limiter)
        with patch.object(server_mod.time, "monotonic", side_effect=[0, 0, 301]), \
             patch.object(server_mod.EVENTS, "get", return_value={"type": "test"}):
            handler.do_GET()
        handler.wfile.write.assert_not_called()
        self.assertEqual(limiter.active, 0)
        self.assertTrue(handler.close_connection)

    def test_handler_rejects_when_budget_is_full(self):
        limiter = _SSEConnectionLimiter(1, 1)
        self.assertTrue(limiter.acquire("synthetic-session"))
        handler = self.make_handler(limiter)
        handler.do_GET()
        handler.send_response.assert_called_once_with(429)
        handler.connection.settimeout.assert_not_called()
        self.assertEqual(limiter.active, 1)

    def test_distinct_verified_sessions_have_distinct_opaque_identities(self):
        handler_class = build_handler("/unused", "/unused")
        handler = object.__new__(handler_class)
        with patch.object(auth_mod, "is_enabled", return_value=True), \
             patch.object(auth_mod, "verify_cookie", return_value="same-user"):
            handler.headers = {"Cookie": f"{auth_mod.COOKIE_NAME}=synthetic-one"}
            first = handler._stream_identity()
            handler.headers = {"Cookie": f"{auth_mod.COOKIE_NAME}=synthetic-two"}
            second = handler._stream_identity()
        self.assertNotEqual(first, second)
        self.assertNotIn("synthetic", first)
        with patch.object(auth_mod, "is_enabled", return_value=True), \
             patch.object(auth_mod, "verify_cookie", return_value=None):
            self.assertEqual(handler._stream_identity(), "unknown")

    def test_enforces_global_and_per_session_caps_and_releases(self):
        limiter = _SSEConnectionLimiter(maximum=3, per_session_maximum=2)

        self.assertTrue(limiter.acquire("alice"))
        self.assertTrue(limiter.acquire("alice"))
        self.assertFalse(limiter.acquire("alice"))
        self.assertTrue(limiter.acquire("bob"))
        self.assertFalse(limiter.acquire("carol"))

        limiter.release("alice")
        self.assertTrue(limiter.acquire("carol"))
        limiter.release("alice")
        limiter.release("bob")
        limiter.release("carol")
        self.assertEqual(limiter.active, 0)
        self.assertEqual(limiter.active_for("alice"), 0)

    def test_release_is_idempotent_when_called_after_all_leases_are_gone(self):
        limiter = _SSEConnectionLimiter(maximum=1, per_session_maximum=1)

        limiter.release("alice")
        self.assertTrue(limiter.acquire("alice"))
        limiter.release("alice")
        limiter.release("alice")
        self.assertEqual(limiter.active, 0)


if __name__ == "__main__":
    unittest.main()
