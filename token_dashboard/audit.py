"""Item #10 — MCP receipt audit-grade persistence.

Every MCP replay receipt is written to `dashboard.mcp_replay` along with
a hash signature pinning it to (call_id, replayed_at, response_tokens,
took_ms, ok). The signature is keyed by an audit secret so receipts can
be later verified against tampering. The verification helper recomputes
the hash and compares.

The audit secret comes from `TD_AUDIT_SECRET` env var; if unset, an
in-memory random key is used (fine for dev — tampering detection still
works within a single process lifetime).
"""
from __future__ import annotations

import hmac
import hashlib
import json
import os
import secrets
import time
from typing import Optional

from . import helios_writer as hw


_audit_key_lock = __import__("threading").Lock()
_audit_key: Optional[bytes] = None


def _key() -> bytes:
    global _audit_key
    with _audit_key_lock:
        if _audit_key is None:
            env = os.environ.get("TD_AUDIT_SECRET")
            _audit_key = env.encode() if env else secrets.token_bytes(32)
    return _audit_key


def _sign(call_id: int, replayed_at: float, response_tokens: Optional[int],
          took_ms: int, ok: bool) -> str:
    msg = f"{call_id}|{replayed_at:.6f}|{response_tokens or 0}|{took_ms}|{1 if ok else 0}"
    return hmac.new(_key(), msg.encode(), hashlib.sha256).hexdigest()


def record(call_id: int, tool_name: str, response_tokens: Optional[int],
           took_ms: int, ok: bool, detail: str = "") -> dict:
    """Insert a signed audit row. Idempotent on call_id (PK)."""
    conn = hw.get_conn()
    if conn is None:
        return {"ok": False, "error": "helios not configured"}
    now = time.time()
    sig = _sign(int(call_id), now, response_tokens, int(took_ms), bool(ok))
    cur = conn.cursor()
    try:
        cur.execute("""
          INSERT INTO dashboard.mcp_replay
            (call_id, tool_name, replayed_at, response_tokens, took_ms, ok,
             detail, audit_signature)
          VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
          ON CONFLICT (call_id) DO UPDATE SET
            tool_name = EXCLUDED.tool_name,
            replayed_at = EXCLUDED.replayed_at,
            response_tokens = EXCLUDED.response_tokens,
            took_ms = EXCLUDED.took_ms,
            ok = EXCLUDED.ok,
            detail = EXCLUDED.detail,
            audit_signature = EXCLUDED.audit_signature
        """, (int(call_id), tool_name, now, response_tokens, int(took_ms),
              1 if ok else 0, detail[:1000], sig))
        hw._commit(conn)
        return {"ok": True, "audit_signature": sig, "replayed_at": now}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def verify(call_id: int) -> dict:
    """Recompute the signature and compare to the stored one."""
    conn = hw.get_conn()
    if conn is None:
        return {"ok": False, "error": "helios not configured"}
    cur = conn.cursor()
    try:
        cur.execute("""
          SELECT call_id, replayed_at, response_tokens, took_ms, ok, audit_signature
            FROM dashboard.mcp_replay WHERE call_id = $1
        """, (int(call_id),))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "no receipt"}
        cid, ts, resp, took, ok_flag, stored_sig = row
        recomputed = _sign(int(cid), float(ts), resp, int(took), bool(ok_flag))
        return {
            "ok": True, "call_id": int(cid),
            "stored_signature": stored_sig,
            "recomputed_signature": recomputed,
            "valid": (recomputed == stored_sig),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def list_receipts(limit: int = 100) -> list:
    conn = hw.get_conn()
    if conn is None:
        return []
    cur = conn.cursor()
    try:
        cur.execute("""
          SELECT call_id, tool_name, replayed_at, response_tokens, took_ms,
                 ok, audit_signature
            FROM dashboard.mcp_replay
           ORDER BY replayed_at DESC
           LIMIT $1
        """, (int(limit),))
        return [
            {"call_id": r[0], "tool_name": r[1],
             "replayed_at": float(r[2]) if r[2] else None,
             "response_tokens": r[3], "took_ms": r[4], "ok": bool(r[5]),
             "audit_signature": r[6]}
            for r in cur.fetchall()
        ]
    except Exception:
        return []
