"""HTTP server: static frontend + JSON endpoints + SSE diff stream."""
from __future__ import annotations

import http.server
import hashlib
import json
import logging
import math
import mimetypes
import queue
import threading
import time
import urllib.parse
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .db import (
    overview_totals, expensive_prompts, project_summary,
    tool_token_breakdown, recent_sessions, session_turns,
    daily_token_breakdown, model_breakdown, skill_breakdown,
    mcp_summary, mcp_per_tool, mcp_recent_calls, mcp_top_callers,
    project_mcp_share, session_mcp_share, prompt_mcp_share,
)

# TD_PRIMARY_STORE=helios routes the analytic read functions through the
# HeliosDB-backed implementation (db_helios.py). Default 'sqlite' keeps the
# existing behaviour (and is still the source of truth for ingestion until
# the dual-write cutover is complete).
import os as _os
if _os.environ.get("TD_PRIMARY_STORE", "sqlite").lower() == "helios":
    from .db_helios import (
        overview_totals, expensive_prompts, project_summary,
        tool_token_breakdown, recent_sessions, session_turns,
        daily_token_breakdown, model_breakdown, skill_breakdown,
        mcp_summary, mcp_per_tool, mcp_recent_calls, mcp_top_callers,
        project_mcp_share, session_mcp_share, prompt_mcp_share,
    )
from .pricing import load_pricing, cost_for, get_plan, set_plan
from .browser_policy import SECURITY_HEADERS
from . import budgets as budgets_mod
from .tips import all_tips, dismiss_tip
from .scanner import scan_dir
from .skills import cached_catalog
from . import mcp as mcp_client
from . import pulse as pulse_mod
from . import baseline as baseline_mod
from . import helios_writer as helios
from . import embedder
from . import clusters as clusters_mod
from . import alerts as alerts_mod
from . import audit as audit_mod
from . import branches as branches_mod
from . import docling_ingest
from . import code_resolution
from . import bulk_migrate
from . import auth as auth_mod
from .request_limits import BoundedThreadingHTTPServer
from .scan_control import ScanBusy, ScanCoordinator
from .login_limits import LoginAttemptLimiter


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
PRICING_JSON = Path(__file__).resolve().parent.parent / "pricing.json"

EVENTS: "queue.Queue[dict]" = queue.Queue()
SCAN_COORDINATOR = ScanCoordinator(helios.close_thread_connection)

MAX_POST_BYTES = 1_000_000  # 1 MB — we only accept tiny JSON bodies (plan, tip key)
MAX_LIMIT = 1000
SSE_MAX_CONNECTIONS = 32
SSE_MAX_CONNECTIONS_PER_SESSION = 4
SSE_MAX_LIFETIME_SECONDS = 300.0
SSE_WRITE_TIMEOUT_SECONDS = 5.0


class _SSEConnectionLimiter:
    def __init__(self, maximum: int, per_session_maximum: int):
        self._maximum = maximum
        self._per_session_maximum = per_session_maximum
        self._active = 0
        self._active_by_session: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, session_identity: str) -> bool:
        with self._lock:
            session_active = self._active_by_session.get(session_identity, 0)
            if self._active >= self._maximum or session_active >= self._per_session_maximum:
                return False
            self._active += 1
            self._active_by_session[session_identity] = session_active + 1
            return True

    def release(self, session_identity: str) -> None:
        with self._lock:
            session_active = self._active_by_session.get(session_identity, 0)
            if session_active <= 0:
                return
            self._active -= 1
            if session_active <= 1:
                self._active_by_session.pop(session_identity, None)
            else:
                self._active_by_session[session_identity] = session_active - 1

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def active_for(self, session_identity: str) -> int:
        with self._lock:
            return self._active_by_session.get(session_identity, 0)


def _send_json(handler, obj, status: int = 200) -> None:
    body = json.dumps(obj, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_error(handler, status: int, msg: str) -> None:
    _send_json(handler, {"error": msg}, status=status)


def _clamp_limit(raw, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, MAX_LIMIT))


def _parse_int(raw, name: str, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(raw, bool) or (isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer())):
        raise ValueError(f"{name} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _parse_float(raw, name: str, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _safe_next(value) -> str:
    if not isinstance(value, str) or not value:
        return "/"
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        return "/"
    return value


def _savings_usd(tokens: int, pricing: dict) -> float:
    """Convert saved tool-output tokens to USD. Tool outputs flow back into
    the assistant's context as input tokens, so we price them at the input
    rate. We use the Sonnet 4.6 input rate as a defensible single number —
    Sonnet is the active default, and using Opus pricing would inflate the
    headline; using Haiku would understate it."""
    rate = (pricing.get("tier_fallback") or {}).get("sonnet", {}).get("input", 3.0)
    return round((tokens or 0) * rate / 1_000_000, 4)


def _serve_static(handler, rel: str) -> None:
    rel = rel.lstrip("/")
    p = (WEB_ROOT / rel).resolve()
    if not str(p).startswith(str(WEB_ROOT.resolve())) or not p.is_file():
        handler.send_response(404)
        handler.end_headers()
        return
    body = p.read_bytes()
    ctype, _ = mimetypes.guess_type(str(p))
    handler.send_response(200)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def build_handler(db_path: str, projects_dir: str):
    pricing = load_pricing(PRICING_JSON)
    login_limiter = LoginAttemptLimiter()
    sse_limiter = _SSEConnectionLimiter(
        SSE_MAX_CONNECTIONS, SSE_MAX_CONNECTIONS_PER_SESSION,
    )
    # Load database identity and powered-by attribution once at app start
    app_db_version = helios.get_nano_version() if helios.is_configured() else ""
    logging.info("Token Dashboard initialized with %s", helios.get_powered_by_message())

    class H(http.server.BaseHTTPRequestHandler):
        def end_headers(self):
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            super().end_headers()

        def log_message(self, fmt, *args):
            pass

        def do_HEAD(self):
            return self.do_GET()

        # ---------------- auth helpers ----------------
        def _is_authenticated(self) -> bool:
            if not auth_mod.is_enabled():
                return True  # Open mode (no creds configured)
            cookies = auth_mod.parse_cookie_header(self.headers.get("Cookie"))
            return auth_mod.verify_cookie(cookies.get(auth_mod.COOKIE_NAME)) is not None

        def _csrf_valid(self) -> bool:
            if not auth_mod.is_enabled():
                return True
            cookies = auth_mod.parse_cookie_header(self.headers.get("Cookie"))
            return auth_mod.verify_csrf(
                cookies.get(auth_mod.COOKIE_NAME),
                self.headers.get(auth_mod.CSRF_HEADER),
            )

        def _stream_identity(self) -> str:
            if not auth_mod.is_enabled():
                return "open-mode"
            cookies = auth_mod.parse_cookie_header(self.headers.get("Cookie"))
            cookie = cookies.get(auth_mod.COOKIE_NAME)
            if not auth_mod.verify_cookie(cookie):
                return "unknown"
            return hashlib.sha256(cookie.encode()).hexdigest()

        def _redirect_to_login(self, next_path: str) -> None:
            self.send_response(302)
            self.send_header("Location", "/login?next=" + urllib.parse.quote(next_path))
            self.end_headers()

        def _serve_login(self, next_path: str = "/", error: str = "") -> None:
            cookie_header = self.headers.get("Cookie")
            csrf_candidates = auth_mod.parse_cookie_values(cookie_header, auth_mod.LOGIN_CSRF_COOKIE_NAME)
            csrf_token = None
            for cand in csrf_candidates:
                csrf_token = auth_mod.extract_valid_login_csrf(cand)
                if csrf_token:
                    break

            need_set_cookie = False
            if not csrf_token:
                csrf_cookie = auth_mod.make_login_csrf()
                csrf_token = csrf_cookie.split(".", 1)[0]
                need_set_cookie = True

            body = auth_mod.render_login(
                error=error,
                next_path=next_path,
                csrf_token=csrf_token,
                db_version=app_db_version,
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if need_set_cookie:
                self.send_header("Set-Cookie", auth_mod.login_csrf_cookie_header(csrf_cookie))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            qs = parse_qs(url.query or "")
            path = url.path
            since = qs.get("since", [None])[0]
            until = qs.get("until", [None])[0]
            # ---- public routes (no auth required) ----
            if path in ("/favicon.ico", "/favicon.png", "/robots.txt"):
                self.send_response(204)
                self.end_headers()
                return
            if path == "/login":
                # Already logged in? Bounce to next.
                if self._is_authenticated():
                    self.send_response(302)
                    self.send_header("Location", _safe_next(qs.get("next", ["/"])[0]))
                    self.end_headers()
                    return
                return self._serve_login(next_path=_safe_next(qs.get("next", ["/"])[0]))
            if path == "/logout":
                return _send_error(self, 405, "logout requires POST")
            # ---- gated routes ----
            if not self._is_authenticated():
                # API requests get 401 JSON; HTML requests get a redirect.
                if path.startswith("/api/"):
                    return _send_error(self, 401, "not authenticated")
                return self._redirect_to_login(self.path)
            if path in ("/", "/index.html"):
                return _serve_static(self, "index.html")
            if path.startswith("/web/"):
                return _serve_static(self, path[5:])
            if path == "/api/overview":
                totals = overview_totals(db_path, since, until)
                cost_usd = 0.0
                for m in model_breakdown(db_path, since, until):
                    c = cost_for(m["model"], m, pricing)
                    if c["usd"] is not None:
                        cost_usd += c["usd"]
                totals["cost_usd"] = round(cost_usd, 4)
                return _send_json(self, totals)
            if path == "/api/prompts":
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                sort = qs.get("sort", ["tokens"])[0]
                rows = expensive_prompts(db_path, limit=limit, sort=sort)
                for r in rows:
                    c = cost_for(r["model"], {
                        "input_tokens": 0, "output_tokens": 0,
                        "cache_read_tokens": r["cache_read_tokens"],
                        "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
                    }, pricing)
                    r["estimated_cost_usd"] = c["usd"]
                # Layer D — attach MCP stats per prompt
                share = prompt_mcp_share(db_path, [r["user_uuid"] for r in rows])
                for r in rows:
                    s = share.get(r["user_uuid"], {})
                    r["mcp_calls"]       = s.get("mcp_calls", 0)
                    r["mcp_tokens"]      = s.get("mcp_tokens", 0)
                    r["baseline_tokens"] = s.get("baseline_tokens", 0)
                    r["savings_tokens"]  = max(0, (r["baseline_tokens"] or 0) - (r["mcp_tokens"] or 0))
                return _send_json(self, rows)
            if path == "/api/projects":
                rows = project_summary(db_path, since, until)
                share = project_mcp_share(db_path, since, until)
                for r in rows:
                    s = share.get(r["project_slug"], {})
                    r["mcp_calls"]       = s.get("mcp_calls", 0)
                    r["mcp_tokens"]      = s.get("mcp_tokens", 0)
                    r["baseline_tokens"] = s.get("baseline_tokens", 0)
                    r["savings_tokens"]  = max(0, r["baseline_tokens"] - r["mcp_tokens"])
                return _send_json(self, rows)
            if path == "/api/tools":
                return _send_json(self, tool_token_breakdown(db_path, since, until))
            if path == "/api/sessions":
                rows = recent_sessions(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                )
                share = session_mcp_share(db_path, since, until)
                for r in rows:
                    s = share.get(r["session_id"], {})
                    r["mcp_calls"]       = s.get("mcp_calls", 0)
                    r["mcp_tokens"]      = s.get("mcp_tokens", 0)
                    r["baseline_tokens"] = s.get("baseline_tokens", 0)
                    r["savings_tokens"]  = max(0, r["baseline_tokens"] - r["mcp_tokens"])
                return _send_json(self, rows)
            if path == "/api/daily":
                return _send_json(self, daily_token_breakdown(db_path, since, until))
            if path == "/api/skills":
                rows = skill_breakdown(db_path, since, until)
                catalog = cached_catalog()
                for r in rows:
                    info = catalog.get(r["skill"])
                    r["tokens_per_call"] = info["tokens"] if info else None
                return _send_json(self, rows)
            if path == "/api/by-model":
                rows = model_breakdown(db_path, since, until)
                for r in rows:
                    c = cost_for(r["model"], r, pricing)
                    r["cost_usd"] = c["usd"]
                    r["cost_estimated"] = c["estimated"]
                return _send_json(self, rows)
            if path.startswith("/api/sessions/"):
                sid = path.rsplit("/", 1)[1]
                return _send_json(self, session_turns(db_path, sid))
            if path == "/api/tips":
                return _send_json(self, all_tips(db_path))
            if path == "/api/plan":
                return _send_json(self, {"plan": get_plan(db_path), "pricing": pricing})
            if path == "/api/csrf":
                cookies = auth_mod.parse_cookie_header(self.headers.get("Cookie"))
                return _send_json(self, {"csrf_token": auth_mod.csrf_token(
                    cookies.get(auth_mod.COOKIE_NAME))})
            if path == "/api/scan":
                return _send_error(self, 405, "scan requires POST")
            if path == "/api/scan/force":
                return _send_error(self, 405, "force scan requires POST")
            # ---------------- MCP / HeliosDB ----------------
            if path == "/api/mcp/summary":
                summary = mcp_summary(db_path, since, until)
                summary["per_tool"] = mcp_per_tool(db_path, since, until)
                summary["top_callers"] = mcp_top_callers(db_path, since, until,
                    limit=_clamp_limit(qs.get("top", ["10"])[0], 10))
                # Pricing context: convert savings tokens to USD using the
                # configured plan and a reasonable input-token price proxy.
                # We use the unweighted average of all model input prices
                # so the "savings" headline is a defensible single number.
                summary["estimated_savings_usd"] = _savings_usd(
                    summary.get("savings_tokens", 0), pricing)
                return _send_json(self, summary)
            if path == "/api/mcp/recent":
                lim = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                return _send_json(self, mcp_recent_calls(db_path, limit=lim))
            if path == "/api/mcp/catalog":
                force = qs.get("force", ["0"])[0] == "1"
                return _send_json(self, mcp_client.get_catalog(force=force))
            if path == "/api/mcp/verify":
                return _send_json(self, mcp_client.conformance_check())
            if path == "/api/mcp/state":
                # Combined "is everything wired up?" check used by the UI to
                # decide what to render or grey out.
                catalog = mcp_client.get_catalog()
                pulse_configured = pulse_mod.is_configured()
                return _send_json(self, {
                    "mcp_configured":   mcp_client.is_configured(),
                    "mcp_endpoint":     mcp_client.http_url(),
                    "pulse_configured": pulse_configured,
                    "catalog_ok":       bool(catalog.get("tools")) and not catalog.get("error"),
                    "catalog_error":    catalog.get("error"),
                    "min_helios_version": mcp_client.MIN_HELIOS_VERSION,
                })
            if path == "/api/pulse":
                return _send_json(self, pulse_mod.pulse())
            if path == "/api/savings/backfill":
                return _send_error(self, 405, "backfill requires POST")
            # ---------------- Path B — HeliosDB-backed features ----------------
            if path == "/api/helios/state":
                # Reuse the cached server_version loaded once at app start
                server_ver = helios.fetch_server_version()
                return _send_json(self, {
                    "configured": helios.is_configured(),
                    "stats": helios.stats() if helios.is_configured() else None,
                    "real_embeddings": embedder.is_real_embedding_available(),
                    "server_version": server_ver,
                    "powered_by": helios.get_powered_by_message(),
                })
            if path == "/api/search":
                # Vector search over prompts (#1)
                q = qs.get("q", [""])[0]
                lim = _clamp_limit(qs.get("limit", ["20"])[0], 20)
                proj = qs.get("project", [None])[0]
                if not q:
                    return _send_json(self, {"error": "missing q"}, 400)
                vec = embedder.embed(q)
                if vec is None:
                    return _send_error(self, 503, "Semantic embeddings unavailable; try again later.")
                rows = helios.search_prompts(vec, limit=lim, project_slug=proj, since=since)
                return _send_json(self, {"query": q, "results": rows,
                                         "real_embeddings": True})
            if path == "/api/explain":
                # RAG-style explain panel (#2)
                uuid = qs.get("uuid", [""])[0]
                if not uuid:
                    return _send_json(self, {"error": "missing uuid"}, 400)
                return _send_json(self, helios.explain_prompt(uuid,
                    neighbours=_clamp_limit(qs.get("neighbours",["5"])[0], 5)))
            if path == "/api/duplicate-retrievals":
                # Item #9
                try:
                    threshold = _parse_float(qs.get("threshold", ["0.05"])[0], "threshold", 0.0, 2.0)
                except ValueError as exc:
                    return _send_error(self, 400, str(exc))
                lim = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                return _send_json(self, helios.find_duplicate_retrievals(threshold=threshold, limit=lim))
            if path == "/api/clusters":
                return _send_json(self, {
                    "clusters": clusters_mod.list_clusters(),
                    "configured": helios.is_configured(),
                })
            if path.startswith("/api/clusters/"):
                try:
                    cid = _parse_int(path.rsplit("/", 1)[1], "cluster id", minimum=0)
                except ValueError as exc:
                    return _send_error(self, 400, str(exc))
                return _send_json(self, clusters_mod.cluster_members(cid,
                    limit=_clamp_limit(qs.get("limit",["50"])[0], 50)))
            if path == "/api/alerts":
                return _send_json(self, alerts_mod.list_alerts(
                    only_open=(qs.get("open",["1"])[0]=="1"),
                    limit=_clamp_limit(qs.get("limit",["50"])[0], 50)))
            if path == "/api/budgets":
                return _send_json(self, {
                    "budgets": budgets_mod.get_budgets(db_path),
                    "exceeded": budgets_mod.check_budgets(
                        db_path, PRICING_JSON, since=since, until=until),
                })
            if path == "/api/branches":
                return _send_json(self, {"snapshots": branches_mod.list_snapshots()})
            if path == "/api/branches/overview":
                br = qs.get("branch", [""])[0]
                if not br:
                    return _send_json(self, {"error": "missing branch"}, 400)
                return _send_json(self, branches_mod.overview_as_of(br))
            if path == "/api/audit":
                return _send_json(self, audit_mod.list_receipts(
                    limit=_clamp_limit(qs.get("limit",["100"])[0], 100)))
            if path == "/api/audit/verify":
                try:
                    cid = _parse_int(qs.get("call_id",["0"])[0], "call_id", minimum=0)
                except ValueError as exc:
                    return _send_error(self, 400, str(exc))
                return _send_json(self, audit_mod.verify(cid))
            if path == "/api/docs":
                return _send_json(self, docling_ingest.list_docs())
            if path == "/api/code-graph/status":
                return _send_json(self, code_resolution.code_graph_status())
            if path == "/api/helios/refresh-mvs":
                return _send_error(self, 405, "refresh requires POST")
            if path == "/api/helios/mv-status":
                from . import helios_mv
                return _send_json(self, {"staleness": helios_mv.staleness()})
            if path == "/api/helios/bulk-migrate":
                return _send_error(self, 405, "migration requires POST")
            if path == "/api/stream":
                session_identity = self._stream_identity()
                if not sse_limiter.acquire(session_identity):
                    return _send_error(self, 429, "too many concurrent streams")
                try:
                    deadline = time.monotonic() + SSE_MAX_LIFETIME_SECONDS
                    self.connection.settimeout(min(SSE_WRITE_TIMEOUT_SECONDS, SSE_MAX_LIFETIME_SECONDS))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return
                        try:
                            evt = EVENTS.get(timeout=min(15.0, remaining))
                            chunk = f"data: {json.dumps(evt, default=str)}\n\n".encode()
                        except queue.Empty:
                            chunk = b": ping\n\n"
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return
                        self.connection.settimeout(min(SSE_WRITE_TIMEOUT_SECONDS, remaining))
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                finally:
                    self.close_connection = True
                    sse_limiter.release(session_identity)
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            url = urlparse(self.path)
            # Login uses form-encoded POSTs — handle before the JSON parser.
            if url.path == "/login":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return _send_error(self, 400, "invalid Content-Length")
                if length < 0 or length > 10000:
                    return _send_error(self, 413, "form too large")
                raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                fields = urllib.parse.parse_qs(raw, keep_blank_values=True)
                cookie_header = self.headers.get("Cookie")
                csrf_candidates = auth_mod.parse_cookie_values(cookie_header, auth_mod.LOGIN_CSRF_COOKIE_NAME)
                if not auth_mod.verify_login_csrf(
                    csrf_candidates,
                    fields.get("csrf_token", [""])[0],
                ):
                    return _send_error(self, 403, "invalid login CSRF token")
                username = (fields.get("username", [""])[0] or "").strip()
                password = fields.get("password", [""])[0] or ""
                next_path = _safe_next(fields.get("next", ["/"])[0])
                peer = str(self.client_address[0])
                lease, retry_after = login_limiter.begin(peer, username)
                if lease is None:
                    body = b'{"error":"Too many login attempts. Try again later."}'
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Retry-After", str(retry_after))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                authenticated = False
                try:
                    authenticated = auth_mod.check_credentials(username, password)
                finally:
                    login_limiter.finish(lease, authenticated)
                    if not authenticated:
                        logging.getLogger(__name__).warning(
                            "Login failed peer_hash=%s account_hash=%s",
                            hashlib.sha256(peer.encode()).hexdigest()[:16],
                            hashlib.sha256(username.encode()).hexdigest()[:16],
                        )
                if authenticated:
                    cookie = auth_mod.make_cookie(username)
                    self.send_response(302)
                    self.send_header("Set-Cookie", auth_mod.cookie_set_header(cookie))
                    self.send_header("Location", next_path)
                    self.end_headers()
                    return
                # Bad creds — re-render the form with an error.
                return self._serve_login(next_path=next_path,
                                         error="Wrong username or password.")
            if url.path == "/logout":
                if not self._is_authenticated():
                    return _send_error(self, 401, "not authenticated")
                if not self._csrf_valid():
                    return _send_error(self, 403, "invalid CSRF token")
                self.send_response(302)
                self.send_header("Set-Cookie", auth_mod.cookie_clear_header())
                self.send_header("Location", "/login")
                self.end_headers()
                return
            # All other POSTs require auth.
            if not self._is_authenticated():
                return _send_error(self, 401, "not authenticated")
            if not self._csrf_valid():
                return _send_error(self, 403, "invalid CSRF token")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return _send_error(self, 400, "invalid Content-Length")
            if length < 0 or length > MAX_POST_BYTES:
                return _send_error(self, 413, f"body too large (max {MAX_POST_BYTES} bytes)")
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except json.JSONDecodeError:
                return _send_error(self, 400, "invalid JSON")
            if not isinstance(body, dict):
                return _send_error(self, 400, "body must be a JSON object")
            if url.path == "/api/scan":
                try:
                    result = SCAN_COORDINATOR.run(lambda: _scan_once(db_path, projects_dir))
                except ScanBusy:
                    return _send_error(self, 409, "scan already running")
                return _send_json(self, result)
            if url.path == "/api/scan/force":
                try:
                    SCAN_COORDINATOR.start(lambda: _background_force_scan(db_path, projects_dir))
                except ScanBusy:
                    return _send_error(self, 409, "scan already running")
                return _send_json(self, {"ok": True, "started": True,
                                         "note": "Full rescan running in background. Watch /api/overview for updates."})
            if url.path == "/api/savings/backfill":
                return _send_json(self, baseline_mod.backfill(db_path))
            if url.path == "/api/helios/refresh-mvs":
                from . import helios_mv
                helios_mv.ensure_mvs()
                return _send_json(self, helios_mv.refresh_all())
            if url.path == "/api/helios/bulk-migrate":
                try:
                    lim = _parse_int(body.get("limit", 5000), "limit", minimum=1, maximum=5000)
                except ValueError as exc:
                    return _send_error(self, 400, str(exc))
                return _send_json(self, bulk_migrate.migrate(db_path, limit_messages=lim))
            if url.path == "/api/plan":
                set_plan(db_path, body.get("plan", "api"))
                return _send_json(self, {"ok": True})
            if url.path == "/api/budgets":
                try:
                    result = budgets_mod.set_budget(
                        db_path, str(body.get("project_slug", "")),
                        body.get("daily_usd"), body.get("monthly_usd"), body.get("webhook"),
                    )
                except (TypeError, ValueError) as exc:
                    return _send_error(self, 400, str(exc))
                return _send_json(self, result)
            if url.path == "/api/tips/dismiss":
                dismiss_tip(db_path, body.get("key", ""))
                return _send_json(self, {"ok": True})
            if url.path == "/api/mcp/replay":
                # Body: {call_id?: int, recent?: int}
                replays = []
                try:
                    timeout = _parse_float(body.get("timeout", 30), "timeout", 0.1, 120.0)
                    if "call_id" in body:
                        call_id = _parse_int(body["call_id"], "call_id", minimum=1)
                    else:
                        n = _parse_int(body.get("recent", 5), "recent", minimum=1, maximum=50)
                except ValueError as exc:
                    return _send_error(self, 400, str(exc))
                if "call_id" in body:
                    replays = [mcp_client.replay_call(db_path, call_id, timeout=timeout)]
                else:
                    replays = mcp_client.replay_recent(db_path, limit=n, timeout=timeout)
                # Mirror replay receipts into the audit log (#10)
                if helios.is_configured():
                    for r in replays:
                        if not isinstance(r, dict) or "call_id" not in r:
                            continue
                        audit_mod.record(int(r["call_id"]), r.get("tool_name") or "?",
                                         r.get("response_tokens"),
                                         int(r.get("took_ms") or 0),
                                         bool(r.get("ok")), r.get("detail") or "")
                return _send_json(self, {"replays": replays})
            if url.path == "/api/clusters/build":
                try:
                    k = _parse_int(body.get("k", 8), "k", minimum=1, maximum=100)
                except ValueError as exc:
                    return _send_error(self, 400, str(exc))
                proj = body.get("project_slug")
                return _send_json(self, clusters_mod.cluster_prompts(k=k, project_slug=proj))
            if url.path == "/api/branches/snapshot":
                return _send_json(self, branches_mod.create_snapshot(body.get("name")))
            if url.path == "/api/alerts/ack":
                try:
                    alert_id = _parse_int(body.get("id", 0), "id", minimum=0)
                except ValueError as exc:
                    return _send_error(self, 400, str(exc))
                return _send_json(self, {"ok": alerts_mod.acknowledge(alert_id)})
            if url.path == "/api/docs/ingest":
                slug = body.get("project_slug") or ""
                if not slug:
                    return _send_error(self, 400, "missing project_slug")
                return _send_json(self, docling_ingest.ingest_project(slug, body.get("cwd")))
            if url.path == "/api/code-graph/index":
                cwd = body.get("cwd") or ""
                if not cwd:
                    return _send_error(self, 400, "missing cwd")
                return _send_json(self, code_resolution.index_project(cwd))
            self.send_response(404)
            self.end_headers()

    return H


def _scan_once(db_path: str, projects_dir: str, force: bool = False):
    if force:
        from .db import connect
        with connect(db_path) as connection:
            connection.execute("DELETE FROM files")
            connection.commit()
    return scan_dir(projects_dir, db_path)


def _background_force_scan(db_path: str, projects_dir: str):
    try:
        return _scan_once(db_path, projects_dir, force=True)
    except Exception as error:
        EVENTS.put({"type": "error", "message": str(error)})


def _scan_loop(db_path: str, projects_dir: str, interval: float = 3600.0):
    while True:
        try:
            n = SCAN_COORDINATOR.run(lambda: _scan_once(db_path, projects_dir))
            if n["messages"] > 0:
                EVENTS.put({"type": "scan", "n": n, "ts": time.time()})
        except ScanBusy:
            pass
        except Exception as e:
            EVENTS.put({"type": "error", "message": str(e)})
        time.sleep(interval)


def run(host: str, port: int, db_path: str, projects_dir: str):
    threading.Thread(target=_scan_loop, args=(db_path, projects_dir), daemon=True).start()
    H = build_handler(db_path, projects_dir)
    httpd = BoundedThreadingHTTPServer((host, port), H, cleanup=helios.close_thread_connection)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
