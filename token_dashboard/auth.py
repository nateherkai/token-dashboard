"""In-app session auth for the dashboard.

Renders an integrated login form inside the app instead of relying on a
reverse proxy for access control. The dashboard's Python server checks a
signed session cookie on every request; missing/invalid cookies get
redirected to /login.

Configuration (env, all required for auth to be enabled):
  TD_AUTH_USER=admin
  TD_AUTH_PASSWORD=<plaintext password>
  TD_AUTH_SECRET=<random 32-byte hex string for signing cookies>

If any of these are missing, the server runs in open mode (no auth) —
fine for a local-only bind (127.0.0.1). Put a reverse proxy or firewall
in front of the dashboard if you expose it beyond localhost.

Cookie shape:
  td_session=<base64(payload)>.<hex(hmac_sha256(secret, payload))>
where payload is `<username>|<expiry_unix_seconds>` for legacy sessions or
`<username>|<expiry_unix_seconds>|<random_csrf_nonce>` for new sessions.
Expiry default 14 days. Tampering changes the HMAC; expiry past wall-clock
invalidates.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional, Union


COOKIE_NAME = "td_session"
LOGIN_CSRF_COOKIE_NAME = "td_login_csrf"
CSRF_HEADER = "X-TD-CSRF"
COOKIE_MAX_AGE = 14 * 24 * 3600  # 14 days
LOGIN_CSRF_MAX_AGE = 10 * 60


def _constant_time_equal(left: object, right: object) -> bool:
    try:
        return hmac.compare_digest(left, right)
    except (TypeError, ValueError):
        return False


def _user() -> Optional[str]:
    return os.environ.get("TD_AUTH_USER")


def _password() -> Optional[str]:
    return os.environ.get("TD_AUTH_PASSWORD")


def _secret() -> Optional[bytes]:
    s = os.environ.get("TD_AUTH_SECRET")
    return s.encode() if s else None


def is_enabled() -> bool:
    """True iff all three config values are set. Open-mode otherwise."""
    return bool(_user() and _password() and _secret())


def _sign(payload: str) -> str:
    return hmac.new(_secret() or b"", payload.encode(), hashlib.sha256).hexdigest()


def make_cookie(username: str) -> str:
    """Build a signed cookie value good for COOKIE_MAX_AGE seconds."""
    expiry = int(time.time()) + COOKIE_MAX_AGE
    payload = f"{username}|{expiry}|{secrets.token_urlsafe(32)}"
    sig = _sign(payload)
    encoded = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{encoded}.{sig}"


def verify_cookie(cookie_value: Optional[str]) -> Optional[str]:
    """Return the authenticated username if the cookie verifies, else None."""
    if not cookie_value or "." not in cookie_value:
        return None
    try:
        encoded, sig = cookie_value.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(encoded.encode()).decode()
    except Exception:
        return None
    expected_sig = _sign(payload)
    # Constant-time compare to avoid timing leaks
    if not _constant_time_equal(sig, expected_sig):
        return None
    if payload.count("|") not in (1, 2):
        return None
    fields = payload.split("|")
    username, expiry_str = fields[:2]
    try:
        if int(expiry_str) < int(time.time()):
            return None
    except ValueError:
        return None
    if username != _user():
        return None
    return username


def csrf_token(cookie_value: Optional[str]) -> Optional[str]:
    if not cookie_value or "." not in cookie_value:
        return None
    try:
        encoded, sig = cookie_value.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(encoded.encode()).decode()
    except Exception:
        return None
    if not _constant_time_equal(sig, _sign(payload)) or payload.count("|") not in (1, 2):
        return None
    fields = payload.split("|")
    username, expiry_str = fields[:2]
    try:
        if int(expiry_str) < int(time.time()):
            return None
    except ValueError:
        return None
    if username != _user():
        return None
    if len(fields) == 2:
        return _sign(f"td-session-csrf-v1\x00{cookie_value}")
    return fields[2] or None


def verify_csrf(cookie_value: Optional[str], submitted: Optional[str]) -> bool:
    token = csrf_token(cookie_value)
    return bool(token and submitted and _constant_time_equal(token, submitted))


def make_login_csrf() -> str:
    token = secrets.token_urlsafe(32)
    payload = f"{token}|{int(time.time()) + LOGIN_CSRF_MAX_AGE}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{token}.{encoded}.{_sign(payload)}"


def extract_valid_login_csrf(cookie_value: Optional[str]) -> Optional[str]:
    """Return the unexpired token if the cookie verifies, else None."""
    if not cookie_value or cookie_value.count(".") != 2:
        return None
    token, encoded, sig = cookie_value.split(".", 2)
    try:
        payload = base64.urlsafe_b64decode(encoded.encode()).decode()
    except Exception:
        return None
    if not _constant_time_equal(sig, _sign(payload)):
        return None
    try:
        payload_token, expiry = payload.split("|", 1)
        if _constant_time_equal(payload_token, token) and int(expiry) >= int(time.time()):
            return token
    except (ValueError, TypeError):
        return None
    return None


def verify_login_csrf(cookie_value: Optional[Union[str, list[str]]], submitted: Optional[str]) -> bool:
    if not cookie_value or not submitted:
        return False
    candidates = [cookie_value] if isinstance(cookie_value, str) else cookie_value
    for cand in candidates:
        token = extract_valid_login_csrf(cand)
        if token and _constant_time_equal(token, submitted):
            return True
    return False


def check_credentials(username: str, password: str) -> bool:
    """Constant-time check of submitted credentials."""
    if not is_enabled():
        return False
    expected_user = _user() or ""
    expected_pw = _password() or ""
    user_ok = _constant_time_equal(username.encode(), expected_user.encode())
    pw_ok = _constant_time_equal(password.encode(), expected_pw.encode())
    return user_ok and pw_ok


def parse_cookie_values(header_value: Optional[str], cookie_name: str) -> list[str]:
    """Extract all values for a specific cookie name from the Cookie header."""
    out: list[str] = []
    if not header_value:
        return out
    for part in header_value.split(";"):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            if k == cookie_name and v:
                out.append(v)
    return out


def parse_cookie_header(header_value: Optional[str]) -> dict:
    """Cheap cookie-header → dict parser (handler.headers.get('Cookie'))."""
    out: dict = {}
    if not header_value:
        return out
    for part in header_value.split(";"):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            out[k] = v
    return out


def cookie_set_header(value: str) -> str:
    """Build the Set-Cookie header. Lax samesite is fine — login is
    same-origin. The Secure flag is set unconditionally; serve the
    dashboard over HTTPS (directly or behind a TLS-terminating proxy)."""
    return (
        f"{COOKIE_NAME}={value}; "
        f"Path=/; HttpOnly; SameSite=Lax; Secure; Max-Age={COOKIE_MAX_AGE}"
    )


def cookie_clear_header() -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Secure; Max-Age=0"


def login_csrf_cookie_header(value: str) -> str:
    return (
        f"{LOGIN_CSRF_COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Lax; Secure; "
        f"Max-Age={LOGIN_CSRF_MAX_AGE}"
    )


# ---------------- HTML ----------------

LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Token Dashboard — sign in</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>⚡</text></svg>">
<style>
  :root {
    --bg: #0A0E14; --panel: #0F1419; --panel-2: #131922;
    --border: #1F2630; --text: #E6EDF3; --muted: #8B98A6;
    --accent: #4A9EFF; --accent-2: #7C5CFF; --bad: #E5484D;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg); color: var(--text); margin: 0;
    font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
    font-size: 14px; line-height: 1.55; min-height: 100vh;
    display: grid; place-items: center;
    background:
      radial-gradient(1200px 600px at 20% 0%, rgba(74,158,255,0.08), transparent 50%),
      radial-gradient(1200px 600px at 80% 100%, rgba(124,92,255,0.08), transparent 50%),
      var(--bg);
  }
  .login-card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 14px; padding: 32px;
    width: 100%; max-width: 380px;
    box-shadow: 0 30px 80px rgba(0,0,0,0.5);
  }
  .brand {
    display: flex; align-items: center; gap: 10px;
    font-weight: 600; font-size: 16px; letter-spacing: -0.01em;
    margin-bottom: 6px;
  }
  .brand::before {
    content: ""; display: inline-block; width: 10px; height: 10px;
    background: var(--accent); border-radius: 3px;
    box-shadow: 0 0 14px var(--accent);
  }
  .sub {
    color: var(--muted); font-size: 12px;
    margin: 0 0 22px;
  }
  label {
    display: block; color: var(--muted);
    font-size: 11px; font-weight: 600; letter-spacing: 0.06em;
    text-transform: uppercase; margin-top: 14px; margin-bottom: 6px;
  }
  input[type=text], input[type=password] {
    width: 100%; padding: 10px 12px;
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text);
    font-family: inherit; font-size: 14px;
    transition: border-color 120ms;
  }
  input[type=text]:focus, input[type=password]:focus {
    outline: none; border-color: var(--accent);
  }
  button {
    width: 100%; margin-top: 22px; padding: 11px 12px;
    background: var(--accent); color: #fff; border: 0;
    border-radius: 8px; font-family: inherit; font-size: 14px; font-weight: 600;
    cursor: pointer; transition: background 120ms;
  }
  button:hover { background: #5BA8FF; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .err {
    color: var(--bad); font-size: 12px; margin-top: 14px;
    min-height: 16px;
  }
  .footer {
    margin-top: 24px; padding-top: 16px;
    border-top: 1px solid var(--border);
    color: var(--muted); font-size: 11px; text-align: center;
  }
  .footer a { color: var(--accent-2); text-decoration: none; }
  .footer a:hover { text-decoration: underline; }
</style>
</head>
<body>
<form class="login-card" method="POST" action="/login" autocomplete="on">
  <div class="brand">Token Dashboard</div>
  <p class="sub">Sign in to continue.</p>
  <label for="username">Username</label>
  <input id="username" name="username" type="text" autofocus autocomplete="username" required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <input type="hidden" name="next" value="__NEXT__">
  <input type="hidden" name="csrf_token" value="__CSRF__">
  <button type="submit">Sign in</button>
  <div class="err">__ERROR__</div>
  <div class="footer">
    __POWERED_BY__
  </div>
</form>
</body>
</html>
"""


def render_login(error: str = "", next_path: str = "/", csrf_token: str = "", db_version: str = "") -> bytes:
    safe_err = (error or "").replace("<", "&lt;").replace(">", "&gt;")
    safe_next = (next_path or "/").replace('"', "%22").replace("<", "")
    ver = f" {db_version}".strip() if db_version else ""
    link_text = f"HeliosDB-Nano {ver}".strip()
    powered_by = f'Powered by <a href="https://github.com/dimensigon/HDB-HeliosDB-Nano/" target="_blank" rel="noopener">{link_text}</a>'
    return (LOGIN_HTML
            .replace("__ERROR__", safe_err)
            .replace("__NEXT__", safe_next)
            .replace("__CSRF__", csrf_token)
            .replace("__POWERED_BY__", powered_by)
            .encode("utf-8"))
