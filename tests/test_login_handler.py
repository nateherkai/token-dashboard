import http.server
import logging
import os
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from token_dashboard import auth, server
from token_dashboard.login_limits import LoginAttemptLimiter


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class LoginHandlerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.env = patch.dict(os.environ, {
            "TD_AUTH_USER": "test-operator",
            "TD_AUTH_PASSWORD": "isolated-test-password",
            "TD_AUTH_SECRET": "isolated-test-signing-secret",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        limiter = LoginAttemptLimiter(clock=lambda: self.now)
        with patch.object(server, "LoginAttemptLimiter", return_value=limiter):
            handler = server.build_handler("/unused", "/unused")
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.opener = urllib.request.build_opener(NoRedirect)
        self.url = "http://127.0.0.1:%d/login" % self.httpd.server_port
        self.cookie = auth.make_login_csrf()
        self.token = self.cookie.split(".", 1)[0]

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def submit(self, username="test-operator", password="incorrect", headers=None, token=None):
        form = urllib.parse.urlencode({
            "username": username, "password": password,
            "csrf_token": self.token if token is None else token,
        }).encode()
        request_headers = {
            "Cookie": auth.LOGIN_CSRF_COOKIE_NAME + "=" + self.cookie,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        request_headers.update(headers or {})
        request = urllib.request.Request(self.url, data=form, headers=request_headers)
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, response.headers, response.read()

    def test_failed_attempts_lock_out_before_credential_check(self):
        with patch.object(auth, "check_credentials", return_value=False) as check:
            with self.assertLogs(server.__name__, level=logging.WARNING) as logs:
                for _ in range(5):
                    self.assertEqual(self.submit()[0], 200)
            status, headers, body = self.submit()
            self.assertEqual(status, 429)
            self.assertEqual(headers["Retry-After"], "60")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertNotIn(b"test-operator", body)
            self.assertEqual(check.call_count, 5)
            self.assertEqual(len(logs.output), 5)
            for entry in logs.output:
                self.assertNotIn("test-operator", entry)
                self.assertNotIn("incorrect", entry)

    def test_forwarded_headers_cannot_reset_peer_bucket(self):
        with patch.object(auth, "check_credentials", return_value=False) as check:
            for number in range(5):
                self.assertEqual(self.submit(username="test-user-%d" % number)[0], 200)
            status, _, _ = self.submit(username="another-test-user", headers={
                "X-Forwarded-For": "192.0.2.20", "X-Real-IP": "192.0.2.21",
            })
            self.assertEqual(status, 429)
            self.assertEqual(check.call_count, 5)

    def test_success_after_lockout_expiry_keeps_session_flow(self):
        for _ in range(5):
            self.assertEqual(self.submit()[0], 200)
        self.assertEqual(self.submit(password="isolated-test-password")[0], 429)
        self.now += 61
        status, headers, _ = self.submit(password="isolated-test-password")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/")
        self.assertIn(auth.COOKIE_NAME + "=", headers["Set-Cookie"])
        self.assertEqual(self.submit()[0], 200)

    def test_invalid_csrf_never_checks_credentials_or_consumes_attempts(self):
        with patch.object(auth, "check_credentials", return_value=False) as check:
            for _ in range(6):
                self.assertEqual(self.submit(token="invalid")[0], 403)
            check.assert_not_called()
            self.assertEqual(self.submit()[0], 200)
            check.assert_called_once()

    def test_existing_session_remains_usable_during_login_lockout(self):
        status, headers, _ = self.submit(password="isolated-test-password")
        self.assertEqual(status, 302)
        session_cookie = headers["Set-Cookie"].split(";", 1)[0]
        for _ in range(5):
            self.submit()
        self.assertEqual(self.submit()[0], 429)
        request = urllib.request.Request(
            self.url.replace("/login", "/api/csrf"), headers={"Cookie": session_cookie},
        )
        with self.opener.open(request, timeout=5) as response:
            self.assertEqual(response.code, 200)
            self.assertIn(b"csrf_token", response.read())

    def test_favicon_returns_204_without_redirect(self):
        request = urllib.request.Request(self.url.replace("/login", "/favicon.ico"))
        with self.opener.open(request, timeout=5) as response:
            self.assertEqual(response.code, 204)
            self.assertNotIn("Location", response.headers)

    def test_serve_login_reuses_valid_existing_csrf_cookie(self):
        request = urllib.request.Request(self.url, headers={"Cookie": f"{auth.LOGIN_CSRF_COOKIE_NAME}={self.cookie}"})
        with self.opener.open(request, timeout=5) as response:
            self.assertEqual(response.code, 200)
            self.assertNotIn("Set-Cookie", response.headers)
            body = response.read().decode("utf-8")
            self.assertIn(f'value="{self.token}"', body)

    def test_multiple_csrf_cookies_verify_matching_token(self):
        stale_cookie = auth.make_login_csrf()
        combined_header = f"{auth.LOGIN_CSRF_COOKIE_NAME}={stale_cookie}; {auth.LOGIN_CSRF_COOKIE_NAME}={self.cookie}"
        status, _, _ = self.submit(headers={"Cookie": combined_header}, token=self.token)
        self.assertEqual(status, 200)

    def test_render_login_footer_dynamic_attribution(self):
        body_with_ver = auth.render_login(db_version="v4.41.0").decode("utf-8")
        self.assertIn("HeliosDB-Nano v4.41.0", body_with_ver)
        self.assertIn("https://github.com/dimensigon/HDB-HeliosDB-Nano/", body_with_ver)

        body_without_ver = auth.render_login(db_version="").decode("utf-8")
        self.assertIn(">HeliosDB-Nano</a>", body_without_ver)


if __name__ == "__main__":
    unittest.main()
