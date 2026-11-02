"""MCP HTTP+SSE client + conformance checker + replay sampler.

Targets the Streamable-HTTP transport per MCP spec 2024-11-05:
  POST <endpoint>     - JSON-RPC requests (response is application/json or
                        text/event-stream depending on server)
  GET  <endpoint>/sse - persistent SSE channel for progress notifications

Stdlib only — uses http.client + ssl + json. No third-party deps.

Configuration via environment:
  MCP_HTTP_URL  — POST endpoint, e.g. https://mcp.example.com/mcp
  MCP_BEARER    — optional bearer token (Authorization: Bearer ...)
"""
from __future__ import annotations

import http.client
import json
import os
import ssl
import time
import uuid
from typing import Optional, Tuple
from urllib.parse import urlparse


MCP_PROTOCOL_VERSION = "2024-11-05"
MIN_HELIOS_VERSION = "3.19.1"


class McpError(Exception):
    pass


def http_url() -> Optional[str]:
    return os.environ.get("MCP_HTTP_URL")


def is_configured() -> bool:
    return bool(http_url())


def _headers(session_id: Optional[str] = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    bearer = os.environ.get("MCP_BEARER")
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


def _request(url: str, method: str, body: Optional[bytes],
             headers: dict, timeout: float) -> Tuple[int, dict, bytes]:
    p = urlparse(url)
    if p.scheme == "https":
        ctx = ssl.create_default_context()
        if os.environ.get("MCP_INSECURE_SKIP_VERIFY") == "1":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(p.hostname, p.port or 443,
                                           timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=timeout)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    status = resp.status
    out_headers = {k.lower(): v for k, v in resp.getheaders()}
    data = resp.read()
    conn.close()
    return status, out_headers, data


def _decode_response(data: bytes, ctype: str) -> dict:
    """Streamable HTTP returns either JSON or a single SSE 'data:' frame."""
    if "event-stream" in ctype:
        for raw in data.decode("utf-8", "replace").splitlines():
            if raw.startswith("data:"):
                payload = raw[5:].strip()
                if payload:
                    return json.loads(payload)
        raise McpError("empty SSE response")
    text = data.decode("utf-8", "replace").strip()
    if not text:
        raise McpError("empty response body")
    return json.loads(text)


def jsonrpc(url: str, method: str, params: Optional[dict] = None,
            session_id: Optional[str] = None,
            request_id: Optional[str] = None,
            timeout: float = 15.0) -> dict:
    """Single JSON-RPC POST. Returns the parsed envelope (with 'result' or 'error')."""
    payload = {
        "jsonrpc": "2.0",
        "id": request_id or str(uuid.uuid4()),
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    body = json.dumps(payload).encode()
    status, hdrs, data = _request(url, "POST", body, _headers(session_id), timeout)
    if status >= 500:
        raise McpError(f"server {status}: {data[:200].decode('utf-8','replace')}")
    return _decode_response(data, hdrs.get("content-type", ""))


def initialize(url: str, timeout: float = 10.0) -> dict:
    resp = jsonrpc(url, "initialize", {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {}, "resources": {}},
        "clientInfo": {"name": "token-dashboard", "version": "1.0.0"},
    }, timeout=timeout)
    if "error" in resp:
        raise McpError(f"initialize error: {resp['error']}")
    return resp.get("result", {})


def list_tools(url: str, timeout: float = 10.0) -> list:
    resp = jsonrpc(url, "tools/list", timeout=timeout)
    if "error" in resp:
        raise McpError(f"tools/list error: {resp['error']}")
    return resp.get("result", {}).get("tools", []) or []


def call_tool(url: str, name: str, arguments: Optional[dict] = None,
              timeout: float = 30.0) -> dict:
    resp = jsonrpc(url, "tools/call",
                   {"name": name, "arguments": arguments or {}},
                   timeout=timeout)
    if "error" in resp:
        raise McpError(f"tools/call({name}) error: {resp['error']}")
    return resp.get("result", {}) or {}


def ping(url: str, timeout: float = 5.0) -> bool:
    try:
        resp = jsonrpc(url, "ping", timeout=timeout)
        return "result" in resp
    except Exception:
        return False


def _semver_ge(actual: Optional[str], required: str) -> bool:
    if not actual:
        return False
    def _parts(v: str):
        out = []
        for piece in v.split("-")[0].split("."):
            try: out.append(int(piece))
            except ValueError: out.append(0)
        while len(out) < 3:
            out.append(0)
        return tuple(out[:3])
    return _parts(actual) >= _parts(required)


def _measure_response_tokens(result: dict) -> int:
    """Best-effort token count of a tools/call result.content payload."""
    chars = 0
    for block in (result.get("content") or []):
        if isinstance(block, dict):
            t = block.get("text")
            if isinstance(t, str):
                chars += len(t)
            elif "data" in block and isinstance(block["data"], str):
                chars += len(block["data"])
    if chars == 0:
        chars = len(json.dumps(result, default=str))
    return chars // 4


def conformance_check(url: Optional[str] = None) -> dict:
    """Run the 8-case MCP 2024-11-05 conformance handshake.

    Cases: initialize, tools/list, tools/call, resources/list, resources/read,
           ping, error_shape, progress_notification.

    Returns a dict with per-case status + summary + version-pin verdict.
    """
    url = url or http_url()
    if not url:
        return {
            "configured": False,
            "reason": "MCP_HTTP_URL not set",
            "cases": [], "summary": {"pass": 0, "fail": 0, "skip": 0},
        }
    cases = []
    server_info: dict = {}
    server_capabilities: dict = {}

    def _add(name, status, detail=""):
        cases.append({"name": name, "status": status, "detail": detail})

    # 1. initialize
    try:
        result = initialize(url)
        server_info = result.get("serverInfo", {}) or {}
        server_capabilities = result.get("capabilities", {}) or {}
        _add("initialize", "pass",
             f"{server_info.get('name','?')} v{server_info.get('version','?')} "
             f"protocol {result.get('protocolVersion','?')}")
    except Exception as e:
        _add("initialize", "fail", str(e)[:200])

    # 2. tools/list
    tools: list = []
    try:
        tools = list_tools(url)
        _add("tools/list", "pass", f"{len(tools)} tools")
    except Exception as e:
        _add("tools/list", "fail", str(e)[:200])

    # 3. tools/call — pick a no-arg tool
    target = None
    for t in tools:
        nm = (t.get("name") or "").lower()
        if nm == "ping" or nm.endswith("/ping") or nm.endswith(".ping"):
            target = t; break
    if not target:
        for t in tools:
            req = ((t.get("inputSchema") or {}).get("required")) or []
            if not req:
                target = t; break
    if target:
        try:
            r = call_tool(url, target["name"], {})
            tokens = _measure_response_tokens(r)
            _add("tools/call", "pass", f"called {target['name']} → ~{tokens} tokens")
        except Exception as e:
            _add("tools/call", "fail", f"{target['name']}: {str(e)[:200]}")
    else:
        _add("tools/call", "skip", "no zero-required-args tool found")

    # 4. resources/list
    declared_resources = "resources" in server_capabilities
    if declared_resources:
        try:
            resp = jsonrpc(url, "resources/list")
            if "error" in resp:
                _add("resources/list", "fail", str(resp["error"])[:200])
            else:
                resources = resp.get("result", {}).get("resources", []) or []
                _add("resources/list", "pass", f"{len(resources)} resources")
                # 5. resources/read — try the first one
                if resources:
                    first_uri = resources[0].get("uri")
                    if first_uri:
                        try:
                            rd = jsonrpc(url, "resources/read", {"uri": first_uri})
                            if "error" in rd:
                                _add("resources/read", "fail", str(rd["error"])[:200])
                            else:
                                contents = rd.get("result", {}).get("contents", []) or []
                                _add("resources/read", "pass", f"{first_uri} → {len(contents)} contents")
                        except Exception as e:
                            _add("resources/read", "fail", str(e)[:200])
                    else:
                        _add("resources/read", "skip", "first resource has no uri")
                else:
                    _add("resources/read", "skip", "no resources to read")
        except Exception as e:
            _add("resources/list", "fail", str(e)[:200])
    else:
        _add("resources/list", "skip", "server didn't declare 'resources' capability")
        _add("resources/read", "skip", "no resources capability")

    # 6. ping
    try:
        ok = ping(url)
        _add("ping", "pass" if ok else "fail", "pong" if ok else "no response")
    except Exception as e:
        _add("ping", "fail", str(e)[:200])

    # 7. error_shape — call a method that doesn't exist
    try:
        resp = jsonrpc(url, "tools/this_method_does_not_exist_for_conformance_check")
        err = resp.get("error")
        if isinstance(err, dict) and "code" in err and "message" in err:
            _add("error_shape", "pass",
                 f"code={err['code']} message={str(err['message'])[:80]}")
        else:
            _add("error_shape", "fail",
                 "expected error.{code,message}, got: " + json.dumps(resp)[:200])
    except Exception as e:
        _add("error_shape", "fail", str(e)[:200])

    # 8. progress_notification — POST tools/call with _meta.progressToken
    if target:
        try:
            payload = {
                "jsonrpc": "2.0", "id": str(uuid.uuid4()),
                "method": "tools/call",
                "params": {
                    "name": target["name"],
                    "arguments": {},
                    "_meta": {"progressToken": f"tok-{int(time.time()*1000)}"},
                },
            }
            status, _, data = _request(url, "POST", json.dumps(payload).encode(),
                                       _headers(), 10.0)
            if status < 400:
                _add("progress_notification", "pass",
                     f"server accepted _meta.progressToken (HTTP {status})")
            else:
                _add("progress_notification", "fail",
                     f"HTTP {status}: {data[:160].decode('utf-8','replace')}")
        except Exception as e:
            _add("progress_notification", "fail", str(e)[:200])
    else:
        _add("progress_notification", "skip", "no target tool")

    server_version = server_info.get("version")
    return {
        "configured": True,
        "endpoint": url,
        "cases": cases,
        "summary": {
            "pass": sum(1 for c in cases if c["status"] == "pass"),
            "fail": sum(1 for c in cases if c["status"] == "fail"),
            "skip": sum(1 for c in cases if c["status"] == "skip"),
        },
        "server_info": server_info,
        "server_capabilities": server_capabilities,
        "server_version": server_version,
        "min_required_version": MIN_HELIOS_VERSION,
        "version_pin_ok": _semver_ge(server_version, MIN_HELIOS_VERSION),
        "limited_mode": not _semver_ge(server_version, MIN_HELIOS_VERSION),
        "checked_at": time.time(),
    }


# ---------- Catalog cache ----------

_CATALOG_CACHE: dict = {"ts": 0.0, "tools": [], "server": {}, "url": None}
_CATALOG_TTL_SEC = 300.0


def get_catalog(force: bool = False) -> dict:
    """Cached `tools/list` from the configured MCP endpoint (5-minute TTL)."""
    url = http_url()
    if not url:
        return {"configured": False, "reason": "MCP_HTTP_URL not set",
                "tools": [], "server": {}}
    now = time.time()
    if (not force and _CATALOG_CACHE["url"] == url
            and (now - _CATALOG_CACHE["ts"]) < _CATALOG_TTL_SEC
            and _CATALOG_CACHE["tools"]):
        return {"configured": True, "endpoint": url, "cached": True,
                "tools": _CATALOG_CACHE["tools"], "server": _CATALOG_CACHE["server"],
                "ts": _CATALOG_CACHE["ts"]}
    try:
        info = initialize(url)
        tools = list_tools(url)
        _CATALOG_CACHE.update({"ts": now, "tools": tools,
                               "server": info.get("serverInfo", {}) or {},
                               "url": url})
        return {"configured": True, "endpoint": url, "cached": False,
                "tools": tools, "server": info.get("serverInfo", {}) or {},
                "ts": now}
    except Exception as e:
        return {"configured": True, "endpoint": url, "cached": False,
                "tools": [], "server": {}, "error": str(e)[:300]}


# ---------- Replay sampler ----------

def replay_call(db_path: str, call_id: int, timeout: float = 30.0) -> dict:
    """Re-issue a stored MCP call against the live endpoint and store the
    response token count in mcp_replay. Used to compare current MCP behaviour
    against the historically recorded result_tokens."""
    from .db import connect
    url = http_url()
    if not url:
        return {"ok": False, "error": "MCP_HTTP_URL not set"}
    with connect(db_path) as c:
        row = c.execute(
            "SELECT id, tool_name, target FROM tool_calls WHERE id=?",
            (call_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"call_id {call_id} not found"}
        tool_name = row["tool_name"]
        # Argument reconstruction is best-effort — use the recorded `target`
        # as a sensible default for the most common arg names.
        arguments: dict = {}
        if row["target"]:
            arguments = {"query": row["target"], "name": row["target"], "symbol": row["target"]}

    started = time.time()
    detail = ""
    response_tokens = None
    ok = False
    try:
        result = call_tool(url, tool_name, arguments, timeout=timeout)
        response_tokens = _measure_response_tokens(result)
        ok = True
        detail = f"~{response_tokens} tokens"
    except McpError as e:
        detail = str(e)[:300]
    except Exception as e:
        detail = f"{type(e).__name__}: {str(e)[:280]}"
    took_ms = int((time.time() - started) * 1000)

    with connect(db_path) as c:
        c.execute("""
          INSERT OR REPLACE INTO mcp_replay
            (call_id, tool_name, replayed_at, response_tokens, took_ms, ok, detail)
          VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (call_id, tool_name, time.time(), response_tokens, took_ms,
              1 if ok else 0, detail))
        if ok and response_tokens is not None:
            c.execute(
                "UPDATE tool_calls SET baseline_method='measured' WHERE id=?",
                (call_id,),
            )
        c.commit()
    return {"ok": ok, "call_id": call_id, "tool_name": tool_name,
            "response_tokens": response_tokens, "took_ms": took_ms,
            "detail": detail}


def replay_recent(db_path: str, limit: int = 5, timeout: float = 30.0) -> list:
    """Replay the N most recent MCP calls that haven't been replayed yet."""
    from .db import connect
    if not is_configured():
        return [{"ok": False, "error": "MCP_HTTP_URL not set"}]
    with connect(db_path) as c:
        rows = c.execute("""
          SELECT t.id FROM tool_calls t
          LEFT JOIN mcp_replay r ON r.call_id = t.id
          WHERE t.tool_name LIKE 'mcp__%' AND r.call_id IS NULL
          ORDER BY t.timestamp DESC
          LIMIT ?
        """, (limit,)).fetchall()
        ids = [r["id"] for r in rows]
    return [replay_call(db_path, i, timeout=timeout) for i in ids]
