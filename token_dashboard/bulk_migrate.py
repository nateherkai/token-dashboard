"""One-shot SQLite → HeliosDB bulk migration.

Pushes the SQLite cache's messages, tool_calls, and tool_results into
the HeliosDB `dashboard.*` schema so the new HeliosDB-backed features
(vector search, clusters, alerts) have a meaningful corpus to work
against immediately, without waiting for the next 60-min scan loop to
incrementally fill HeliosDB.

Idempotent — re-running is safe; ON CONFLICT clauses upsert.
"""
from __future__ import annotations

import time
from typing import Optional

from . import helios_writer as hw
from . import embedder
from .db import connect as sqlite_connect


def _sanitize_text(s):
    """Drop control chars + cap length so HeliosDB's string-literal parser
    doesn't trip on huge or weird payloads. The dashboard never displays
    >1k chars per prompt anyway."""
    if s is None:
        return None
    # Strip NUL and other control chars except tab/newline/CR
    cleaned = "".join(ch for ch in s if ord(ch) >= 32 or ch in "\t\n\r")
    return cleaned[:50000]


def migrate(db_path: str, limit_messages: int = 5000,
            embed_user_min_chars: int = 40,
            embed_result_min_tokens: int = 500) -> dict:
    """Pull messages + tool_calls + tool_results from SQLite into HeliosDB.

    `limit_messages` caps total rows so the operation stays bounded for big
    caches. Most-recent-first order so the dashboard sees current data first.
    """
    if not hw.is_configured():
        return {"ok": False, "error": "helios not configured"}
    h = hw.get_conn()
    if h is None:
        return {"ok": False, "error": "helios connection failed"}
    started = time.time()
    n_msg = n_tc = n_tr = n_emb_msg = n_emb_tr = 0
    n_msg_fail = n_tc_fail = n_tr_fail = 0
    first_errors: list[str] = []
    def _record_err(prefix: str, e: Exception):
        if len(first_errors) < 5:
            first_errors.append(f"{prefix}: {type(e).__name__}: {str(e)[:200]}")
    with sqlite_connect(db_path) as s:
        # Messages — most recent first
        s_cur = s.execute("""
          SELECT uuid, parent_uuid, session_id, project_slug, cwd, type,
                 timestamp, model, message_id,
                 input_tokens, output_tokens, cache_read_tokens,
                 cache_create_5m_tokens, cache_create_1h_tokens,
                 prompt_text, prompt_chars, tool_calls_json
            FROM messages
           ORDER BY timestamp DESC
           LIMIT ?
        """, (limit_messages,))
        msg_uuids: set[str] = set()
        # Progress every N rows so the script isn't silent for 30 min.
        progress_every = 5000
        progress_t0 = time.time()
        for row in s_cur:
            msg = dict(row)
            # Sanitize prompt_text — HeliosDB v3.26 fails to parse SQL when
            # values contain unescaped control chars or exceed certain sizes.
            msg["prompt_text"] = _sanitize_text(msg.get("prompt_text"))
            msg["tool_calls_json"] = _sanitize_text(msg.get("tool_calls_json"))
            if n_msg and n_msg % progress_every == 0:
                rate = n_msg / max(time.time() - started, 0.01)
                print(f"  [phase 1] msgs pushed={n_msg} fail={n_msg_fail} embedded={n_emb_msg} rate={rate:.0f}/s", flush=True)
            msg_uuids.add(msg["uuid"])
            vec = None
            text = msg.get("prompt_text") or ""
            if msg.get("type") == "user" and len(text) >= embed_user_min_chars:
                try:
                    vec = embedder.embed(text)
                    if vec is not None:
                        n_emb_msg += 1
                except Exception:
                    vec = None
            try:
                hw.upsert_message(h, msg, embedding=vec)
                n_msg += 1
            except Exception as e:
                n_msg_fail += 1
                _record_err("msg", e)
                # If the connection died, do ONE reset and continue. Don't
                # spin reconnects per row — that triggers HeliosDB TCP-accept
                # contention.
                if hw._is_connection_dead(e):
                    import time as _t
                    _t.sleep(2.0)
                    hw._reset_conn()
                    try:
                        h = hw.get_conn()
                    except Exception:
                        return {"ok": False,
                                "error": "helios connection lost mid-migrate (msg)",
                                "messages_pushed": n_msg, "messages_failed": n_msg_fail,
                                "first_errors": first_errors}

        # tool_calls (origin rows, paired with results by tool_use_id) —
        # chunk the IN clause to avoid SQLite's 999-parameter limit.
        BATCH = 500
        msg_uuids_list = list(msg_uuids)
        n_chunks = (len(msg_uuids_list) + BATCH - 1) // BATCH
        print(f"  [phase 1 done] msgs pushed={n_msg} fail={n_msg_fail}; "
              f"starting phase 2 (tool_calls + tool_results) over {n_chunks} chunks", flush=True)
        for ci, offset in enumerate(range(0, len(msg_uuids_list), BATCH)):
            if ci and ci % 50 == 0:
                rate = (n_tc + n_tr) / max(time.time() - started, 0.01)
                print(f"  [phase 2] chunk {ci}/{n_chunks}  tcs={n_tc} trs={n_tr}  combined rate={rate:.0f}/s", flush=True)
            chunk = msg_uuids_list[offset:offset + BATCH]
            placeholders = ",".join(["?"] * len(chunk))
            s_cur = s.execute(f"""
              SELECT id, message_uuid, session_id, project_slug, tool_name,
                     target, tool_use_id, result_tokens, is_error, timestamp,
                     baseline_tokens, baseline_method, resolution, mcp_meta_json
                FROM tool_calls
               WHERE message_uuid IN ({placeholders})
            """, tuple(chunk))
            for row in s_cur:
                tc = dict(row)
                if tc["tool_name"] == "_tool_result":
                    n_tr_inc = _push_tool_result(h, tc)
                    n_tr += n_tr_inc[0]
                    n_emb_tr += n_tr_inc[1]
                else:
                    try:
                        hw.upsert_tool_call(h, tc)
                        n_tc += 1
                    except Exception as e:
                        n_tc_fail += 1
                        _record_err("tc", e)
                        if hw._is_connection_dead(e):
                            hw._reset_conn()
                            h = hw.get_conn()
                            if h is None:
                                return {"ok": False,
                                        "error": "helios connection lost mid-migrate (tc)",
                                        "messages_pushed": n_msg, "tool_calls_pushed": n_tc,
                                        "first_errors": first_errors}
    return {
        "ok": True,
        "elapsed_s": round(time.time() - started, 1),
        "messages_pushed": n_msg,
        "messages_failed": n_msg_fail,
        "messages_embedded": n_emb_msg,
        "tool_calls_pushed": n_tc,
        "tool_calls_failed": n_tc_fail,
        "tool_results_pushed": n_tr,
        "tool_results_embedded": n_emb_tr,
        "first_errors": first_errors,
    }


def _push_tool_result(h, sqlite_row: dict) -> tuple[int, int]:
    """tool_results in SQLite don't carry body_text (we only added it to
    scanner.py's parse output, not to the SQLite schema). So at bulk-migration
    time we can't embed historical results — but we can still mirror the
    metadata so the dashboard has the full structure available."""
    row = {
        "tool_use_id":  sqlite_row.get("tool_use_id") or sqlite_row.get("target"),
        "message_uuid": sqlite_row["message_uuid"],
        "session_id":   sqlite_row["session_id"],
        "project_slug": sqlite_row["project_slug"],
        "result_tokens": sqlite_row.get("result_tokens"),
        "is_error":     sqlite_row.get("is_error", 0),
        "timestamp":    sqlite_row["timestamp"],
        "body_text":    None,  # not available historically
    }
    if not row["tool_use_id"]:
        return (0, 0)
    try:
        hw.upsert_tool_result(h, row, embedding=None)
        return (1, 0)
    except Exception:
        return (0, 0)
