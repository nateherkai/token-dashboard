"""JSONL transcript walker + parser."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional, Tuple, Union

from .db import connect


INSERT_MSG = """
INSERT OR REPLACE INTO messages (
  uuid, parent_uuid, session_id, project_slug, cwd, git_branch, cc_version, entrypoint,
  type, is_sidechain, agent_id, timestamp, model, stop_reason, prompt_id, message_id,
  input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
  prompt_text, prompt_chars, tool_calls_json
) VALUES (
  :uuid, :parent_uuid, :session_id, :project_slug, :cwd, :git_branch, :cc_version, :entrypoint,
  :type, :is_sidechain, :agent_id, :timestamp, :model, :stop_reason, :prompt_id, :message_id,
  :input_tokens, :output_tokens, :cache_read_tokens, :cache_create_5m_tokens, :cache_create_1h_tokens,
  :prompt_text, :prompt_chars, :tool_calls_json
)
"""

INSERT_TOOL = """
INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, tool_use_id, result_tokens, is_error, timestamp)
VALUES (:message_uuid, :session_id, :project_slug, :tool_name, :target, :tool_use_id, :result_tokens, :is_error, :timestamp)
"""


_TARGET_FIELDS = {
    "Read":      "file_path",
    "Edit":      "file_path",
    "Write":     "file_path",
    "Glob":      "pattern",
    "Grep":      "pattern",
    "Bash":      "command",
    "WebFetch":  "url",
    "WebSearch": "query",
    "Task":      "subagent_type",
    "Skill":     "skill",
}


def _usage(rec: dict) -> dict:
    u = (rec.get("message") or {}).get("usage") or {}
    cc = u.get("cache_creation") or {}
    return {
        "input_tokens":           int(u.get("input_tokens") or 0),
        "output_tokens":          int(u.get("output_tokens") or 0),
        "cache_read_tokens":      int(u.get("cache_read_input_tokens") or 0),
        "cache_create_5m_tokens": int(cc.get("ephemeral_5m_input_tokens") or 0),
        "cache_create_1h_tokens": int(cc.get("ephemeral_1h_input_tokens") or 0),
    }


def _prompt_text(rec: dict) -> Tuple[Optional[str], Optional[int]]:
    if rec.get("type") != "user":
        return None, None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content, len(content)
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "".join(parts) if parts else None
        return text, (len(text) if text else None)
    return None, None


def _target(name: str, inp: dict) -> Optional[str]:
    field = _TARGET_FIELDS.get(name)
    if field and isinstance(inp, dict):
        v = inp.get(field)
        if isinstance(v, str):
            return v[:500]
    return None


def _extract_tools(rec: dict) -> List[dict]:
    out = []
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name") or "unknown"
        target = _target(name, block.get("input") or {})
        out.append({
            "tool_name":     name,
            "target":        target,
            "tool_use_id":   block.get("id"),
            "result_tokens": None,
            "is_error":      0,
            "timestamp":     rec.get("timestamp"),
        })
    return out


def _extract_results(rec: dict) -> List[dict]:
    out = []
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        body = block.get("content")
        if isinstance(body, str):
            chars = len(body)
            body_text = body
        elif isinstance(body, list):
            text_parts = [p.get("text", "") for p in body if isinstance(p, dict)]
            body_text = "".join(text_parts)
            chars = len(body_text)
        else:
            chars = 0
            body_text = None
        use_id = block.get("tool_use_id")
        out.append({
            "tool_name":     "_tool_result",
            "target":        use_id,        # legacy: target=tool_use_id on result rows
            "tool_use_id":   use_id,        # new: same value, but in the proper column
            "result_tokens": chars // 4,
            "is_error":      1 if block.get("is_error") else 0,
            "timestamp":     rec.get("timestamp"),
            # Captured for HeliosDB-side embedding (#9). Bounded so we
            # don't blow up the cache for huge tool results.
            "body_text":     body_text[:16_000] if body_text else None,
        })
    return out


def parse_record(rec: dict, project_slug: str) -> Tuple[dict, List[dict]]:
    """Return (message_row, [tool_call_rows])."""
    msg_obj = rec.get("message") or {}
    text, chars = _prompt_text(rec)
    msg = {
        "uuid":         rec.get("uuid"),
        "parent_uuid":  rec.get("parentUuid"),
        "session_id":   rec.get("sessionId"),
        "project_slug": project_slug,
        "cwd":          rec.get("cwd"),
        "git_branch":   rec.get("gitBranch"),
        "cc_version":   rec.get("version"),
        "entrypoint":   rec.get("entrypoint"),
        "type":         rec.get("type"),
        "is_sidechain": 1 if rec.get("isSidechain") else 0,
        "agent_id":     rec.get("agentId"),
        "timestamp":    rec.get("timestamp"),
        "model":        msg_obj.get("model"),
        "stop_reason":  msg_obj.get("stop_reason"),
        "prompt_id":    rec.get("promptId"),
        "message_id":   msg_obj.get("id"),
        "prompt_text":  text,
        "prompt_chars": chars,
        "tool_calls_json": None,
        **_usage(rec),
    }
    tools = _extract_tools(rec)
    tools.extend(_extract_results(rec))
    if tools:
        msg["tool_calls_json"] = json.dumps(
            [{"name": t["tool_name"], "target": t["target"]} for t in tools if t["tool_name"] != "_tool_result"]
        )
    for t in tools:
        t["message_uuid"] = msg["uuid"]
        t["session_id"]   = msg["session_id"]
        t["project_slug"] = project_slug
    return msg, tools


def _project_slug(file_path: Path, projects_root: Path) -> str:
    rel = file_path.relative_to(projects_root)
    return rel.parts[0]


def _evict_prior_snapshots(conn, session_id: str, message_id: str, keep_uuid: str) -> list:
    """Remove older streaming snapshots for the same (session_id, message_id).

    Claude Code writes 2–3 JSONL lines per assistant response (partial → final)
    with identical message.id but distinct top-level uuids. Only the final
    tally matches billing, so earlier snapshots must be replaced, not summed.

    Returns the evicted uuids so the caller can mirror the same eviction onto
    HeliosDB (batched — see scan_dir; per-row DELETE is too slow there).
    """
    old = [r[0] for r in conn.execute(
        "SELECT uuid FROM messages WHERE session_id=? AND message_id=? AND uuid!=?",
        (session_id, message_id, keep_uuid),
    )]
    if not old:
        return []
    placeholders = ",".join("?" * len(old))
    conn.execute(f"DELETE FROM tool_calls WHERE message_uuid IN ({placeholders})", old)
    conn.execute(f"DELETE FROM messages WHERE uuid IN ({placeholders})", old)
    return old


def scan_file(path: Path, project_slug: str, conn, start_byte: int = 0,
              helios_sink=None) -> dict:
    """Ingest new lines from a JSONL file starting at ``start_byte``.

    Returns message/tool counts plus ``end_offset`` — the byte offset just
    past the last fully-parsed line. Callers persist ``end_offset`` as the
    file's high-water mark so a line partially flushed at EOF gets re-read
    once it completes.

    `helios_sink`, when provided, is a callback (msg_dict, tool_list) that
    pushes the parsed record into the HeliosDB mirror in addition to the
    SQLite writes here. Failures in the sink are swallowed — SQLite stays
    authoritative.
    """
    msgs = tools = 0
    evicted: list = []
    end_offset = start_byte
    with open(path, "rb") as fb:
        if start_byte:
            fb.seek(start_byte)
        while True:
            raw = fb.readline()
            if not raw:
                break  # EOF
            if not raw.endswith(b"\n"):
                # Partial line — Claude Code is mid-flush. Leave the
                # high-water mark behind the line start so we re-read it
                # once the write completes.
                break
            line_end = fb.tell()
            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                end_offset = line_end
                continue
            if not line:
                end_offset = line_end
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                end_offset = line_end
                continue
            if not isinstance(rec, dict) or "uuid" not in rec or "type" not in rec:
                end_offset = line_end
                continue
            msg, tlist = parse_record(rec, project_slug)
            if not msg["session_id"] or not msg["timestamp"]:
                end_offset = line_end
                continue
            if msg["message_id"]:
                evicted += _evict_prior_snapshots(conn, msg["session_id"], msg["message_id"], msg["uuid"])
            conn.execute(INSERT_MSG, msg)
            # tool_calls has no natural unique key; clear any prior rows for
            # this uuid so full rescans stay idempotent instead of
            # duplicating rows.
            conn.execute("DELETE FROM tool_calls WHERE message_uuid=?", (msg["uuid"],))
            for t in tlist:
                conn.execute(INSERT_TOOL, t)
                tools += 1
            msgs += 1
            end_offset = line_end
            if helios_sink is not None:
                try:
                    helios_sink(msg, tlist)
                except Exception as e:
                    # Mirror failures are non-fatal. They get retried via
                    # the catch-up path that runs after each scan_dir.
                    pass
    return {"messages": msgs, "tools": tools, "end_offset": end_offset,
            "evicted": evicted}


def scan_dir(projects_root: Union[str, Path], db_path: Union[str, Path]) -> dict:
    root = Path(projects_root)
    totals = {"messages": 0, "tools": 0, "files": 0,
              "backfill": None, "helios": None, "alerts": 0}
    if not root.is_dir():
        return totals
    evicted_uuids: list = []

    # Build the HeliosDB sink once per scan iteration. Same connection is
    # reused across all parsed records.
    sink = _build_helios_sink()

    with connect(db_path) as conn:
        for p in root.rglob("*.jsonl"):
            try:
                stat = p.stat()
            except OSError:
                continue
            row = conn.execute(
                "SELECT mtime, bytes_read FROM files WHERE path=?", (str(p),)
            ).fetchone()
            offset = 0
            if row and row["mtime"] == stat.st_mtime and row["bytes_read"] == stat.st_size:
                continue
            if row and stat.st_size > row["bytes_read"]:
                offset = row["bytes_read"]
            slug = _project_slug(p, root)
            sub = scan_file(p, slug, conn, start_byte=offset, helios_sink=sink)
            # Persist the byte offset of the last fully-parsed line (not
            # st_size) so a partial line mid-flush is retried on the next
            # scan instead of being skipped over.
            conn.execute(
                "INSERT OR REPLACE INTO files (path, mtime, bytes_read, scanned_at) VALUES (?, ?, ?, ?)",
                (str(p), stat.st_mtime, sub["end_offset"], time.time()),
            )
            totals["messages"] += sub["messages"]
            totals["tools"]    += sub["tools"]
            totals["files"]    += 1
            evicted_uuids.extend(sub.get("evicted") or [])
        conn.commit()

    # Mirror SQLite's streaming-snapshot eviction onto HeliosDB in one batch.
    # Each snapshot was already mirrored individually by the sink; here we drop
    # the superseded ones so HeliosDB's token SUMs match SQLite. Batched (not
    # per-message) because DELETE on HeliosDB v3.33 costs ~1s/statement.
    if sink is not None and evicted_uuids:
        try:
            from . import helios_writer as hw
            n = hw.delete_messages(hw.get_conn(), evicted_uuids)
            totals["helios"] = {"evicted_mirrored": n}
        except Exception as e:
            totals["helios"] = {"evict_err": f"{type(e).__name__}: {e}"}

    # Populate baseline_tokens / followup_within_turn for any new tool_calls.
    # Done outside the long-held scanner connection so the backfill's own
    # transactions don't pile up behind the ingest writes.
    if totals["messages"] > 0 or totals["tools"] > 0:
        from .baseline import backfill
        try:
            totals["backfill"] = backfill(str(db_path))
        except Exception as e:
            totals["backfill"] = {"error": f"{type(e).__name__}: {e}"}
    # Live-alerts pass — only useful if HeliosDB is configured.
    if sink is not None:
        try:
            from . import alerts as alerts_mod
            new_alerts = alerts_mod.scan_for_alerts()
            totals["alerts"] = len(new_alerts)
        except Exception as e:
            totals["alerts"] = f"alerts-err:{e}"
    try:
        from . import budgets as budgets_mod
        totals["budget_alerts"] = len(budgets_mod.check_budgets(
            db_path, Path(__file__).resolve().parent.parent / "pricing.json"))
    except Exception as e:
        totals["budget_alerts"] = f"budget-err:{e}"
    if sink is not None:
        # Keep the rollup MVs current. ensure is idempotent; refresh only
        # when this scan actually ingested new rows.
        try:
            from . import helios_mv
            helios_mv.ensure_mvs()
            if totals["messages"] > 0 or totals["tools"] > 0:
                totals["mv_refresh"] = helios_mv.refresh_all()
        except Exception as e:
            totals["mv_refresh"] = {"error": f"{type(e).__name__}: {e}"}
    return totals


def _build_helios_sink():
    """Return a callable (msg, tool_list) → None that mirrors writes to
    HeliosDB, or None if HeliosDB is not configured."""
    try:
        from . import helios_writer as hw
        from . import embedder
    except Exception:
        return None
    if not hw.is_configured():
        return None
    conn = hw.get_conn()
    if conn is None:
        return None

    # Only embed text above this length — saves embedding budget on tiny
    # turns ("ok", "thanks", etc.).
    MIN_EMBED_CHARS = 40
    # Tool-result body embedded only if its rough token count exceeds this
    # — small results don't carry duplicate-detection signal.
    MIN_RESULT_TOKENS_FOR_EMBED = 500

    def sink(msg, tool_list):
        # 1. Mirror the message
        body_vec = None
        text = msg.get("prompt_text") or ""
        if msg.get("type") == "user" and len(text) >= MIN_EMBED_CHARS:
            try:
                body_vec = embedder.embed(text)
            except Exception:
                body_vec = None
        try:
            hw.upsert_message(conn, msg, embedding=body_vec)
        except Exception:
            pass
        # 2. Mirror tool_calls + tool_results from this turn
        for t in tool_list:
            if t["tool_name"] == "_tool_result":
                vec = None
                btext = t.get("body_text")
                if btext and (t.get("result_tokens") or 0) >= MIN_RESULT_TOKENS_FOR_EMBED:
                    try:
                        vec = embedder.embed(btext)
                    except Exception:
                        vec = None
                try:
                    hw.upsert_tool_result(conn, t, embedding=vec)
                except Exception:
                    pass
            else:
                # tool_use — id comes from the SQLite autoincrement; we
                # synthesise a stable BIGINT from (message_uuid, tool_use_id)
                # so re-ingest doesn't duplicate.
                stable_id = abs(hash(("tool", msg["uuid"], t.get("tool_use_id") or t["tool_name"]))) % (2**62)
                try:
                    hw.upsert_tool_call(conn, {**t,
                        "id": stable_id,
                        "message_uuid": msg["uuid"],
                        "session_id": msg["session_id"],
                        "project_slug": msg["project_slug"],
                    })
                except Exception:
                    pass
    return sink
