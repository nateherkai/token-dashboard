"""HeliosDB-as-primary read layer — drop-in replacement for db.py functions.

Activated via the env var `TD_PRIMARY_STORE=helios`. When set, server.py
routes `overview_totals`, `expensive_prompts`, etc. through this module
instead of the SQLite original. The function signatures match db.py so
the routes don't need to know which backend is serving them.

SQL-dialect deltas vs SQLite (the actually-relevant ones):
  - `?` placeholders → `$N`
  - `INSERT OR REPLACE` → `INSERT … ON CONFLICT (…) DO UPDATE`
  - `SUBSTRING(s FROM x FOR y)` → not supported in v3.26 — use `SUBSTR(s, x, y)`
  - CTEs combined with `$N` parameter binding silently return zero rows in
    v3.26 — keep CTEs separate from parameterised filtering.
  - Implicit transactions need explicit `COMMIT` after writes — the
    helios_writer module handles that for the writer path.
"""
from __future__ import annotations

import re
from typing import Optional

from . import helios_writer as hw


# ---------- helpers ----------

def _range_clause(since, until, col: str = "timestamp"):
    where, args = [], []
    n = 1
    if since:
        where.append(f"{col} >= ${n}"); args.append(since); n += 1
    if until:
        where.append(f"{col} < ${n}"); args.append(until); n += 1
    return ((" AND " + " AND ".join(where)) if where else "", args)


# HeliosDB/pg8000 returns aggregate results (SUM, COUNT) as *strings*, while
# stored MV/table columns come back as proper ints. Downstream code multiplies
# these by float prices (pricing.cost_for) and divides them, so a string here
# raises "can't multiply sequence by non-int of type 'float'" and crashes the
# request. Coerce by column name when building result dicts — a no-op on the
# already-int MV reads, a fix on the live/ranged aggregate paths. Also yields
# correct JSON number types to the frontend instead of quoted strings.
_NUMERIC_FIELDS = frozenset({
    "sessions", "turns", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_create_5m_tokens", "cache_create_1h_tokens", "cache_create_tokens",
    "billable_tokens", "tokens", "calls", "invocations", "result_tokens",
    "prompt_chars", "baseline_tokens", "is_sidechain",
})


def _drow(cols, values) -> dict:
    """dict(zip(cols, values)) with numeric columns coerced to int/float."""
    out = {}
    for k, v in zip(cols, values):
        if k in _NUMERIC_FIELDS and isinstance(v, str):
            try:
                v = int(v)
            except ValueError:
                try:
                    v = float(v)
                except ValueError:
                    pass
        out[k] = v
    return out


def _num(v):
    """Coerce a single scalar aggregate value to int (str/Decimal → int)."""
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return v
    return v


def _encode_slug(path: str) -> str:
    return re.sub(r"[:\\/ ]", "-", path)


def _walk_to_root(cwd: str, slug: str) -> Optional[str]:
    if not cwd or not slug:
        return None
    trimmed = cwd.rstrip("/\\")
    sep = "\\" if "\\" in trimmed else "/"
    parts = trimmed.split(sep)
    for i in range(len(parts), 0, -1):
        if _encode_slug(sep.join(parts[:i])) == slug:
            name = parts[i - 1]
            if name:
                return name
    return None


def project_name_for(cwd: Optional[str], fallback_slug: str) -> str:
    name = _walk_to_root(cwd or "", fallback_slug or "")
    if name:
        return name
    if cwd:
        trimmed = cwd.rstrip("/\\")
        sep = "\\" if "\\" in trimmed else "/"
        tail = trimmed.split(sep)[-1]
        if tail:
            return tail
    if fallback_slug:
        parts = [p for p in re.split(r"-+", fallback_slug) if p]
        if parts:
            return parts[-1]
    return fallback_slug or ""


def best_project_name(cwds, slug: str) -> str:
    cwds = [c for c in (cwds or []) if c]
    for cwd in cwds:
        name = _walk_to_root(cwd, slug)
        if name:
            return name
    return project_name_for(cwds[0] if cwds else None, slug)


def _conn():
    """Get the HeliosDB connection. Schema bootstrapped on first call."""
    return hw.get_conn()


def _resolve_project_names(db_path, slugs: list) -> dict:
    """Build {slug: pretty_name} from the SQLite mirror's distinct cwds.

    Name resolution needs the set of cwds per project. On HeliosDB a
    GROUP-BY-without-aggregate MV doesn't dedupe (materializes all 453k
    rows), so resolving there means scanning the full table. SQLite's
    indexed `idx_messages_project` makes the distinct-cwd lookup sub-10ms,
    and SQLite is always populated (the existing primary), so we resolve
    names there regardless of read backend."""
    out: dict[str, str] = {}
    if not slugs:
        return out
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            ph = ",".join("?" * len(slugs))
            cwds: dict[str, list] = {}
            for slug, cwd in conn.execute(
                f"SELECT DISTINCT project_slug, cwd FROM messages "
                f"WHERE project_slug IN ({ph}) AND cwd IS NOT NULL", tuple(slugs)):
                cwds.setdefault(slug, []).append(cwd)
        finally:
            conn.close()
        for slug in slugs:
            out[slug] = best_project_name(cwds.get(slug, []), slug)
    except Exception:
        for slug in slugs:
            out[slug] = best_project_name([], slug)
    return out


# ---------- API-surface functions (mirror db.py signatures) ----------

_OVERVIEW_KEYS = ["sessions", "turns", "input_tokens", "output_tokens",
                  "cache_read_tokens", "cache_create_5m_tokens", "cache_create_1h_tokens"]


def overview_totals(_db_path, since=None, until=None) -> dict:
    cur = _conn().cursor()
    # All-time → single-row MV (exact, <1ms).
    if not since and not until:
        try:
            cur.execute("SELECT sessions, turns, input_tokens, output_tokens, "
                        "cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens "
                        "FROM td_overview")
            row = cur.fetchone()
            if row:
                return _drow(_OVERVIEW_KEYS, row)
        except Exception:
            pass  # fall through to live
    # Ranged → sum the day-grain MV. Token/turn fields are additive across
    # days; `sessions` is summed from per-day distinct counts (a small
    # overcount for sessions spanning >1 day — acceptable for a ranged KPI).
    if since or until:
        day_where, day_args = _range_clause(since, until, col="day")
        try:
            sql = f"""
              SELECT COALESCE(SUM(sessions),0), COALESCE(SUM(turns),0),
                     COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                     COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_create_tokens),0)
                FROM td_overview_daily WHERE 1=1 {day_where}
            """
            if day_args:
                cur.execute(sql, tuple(day_args))
            else:
                cur.execute(sql)
            r = cur.fetchone()
            if r:
                # cache_create_tokens is the combined 5m+1h on the daily MV;
                # split is not preserved, so report it under 5m and 0 for 1h.
                return {"sessions": _num(r[0]), "turns": _num(r[1]),
                        "input_tokens": _num(r[2]), "output_tokens": _num(r[3]),
                        "cache_read_tokens": _num(r[4]),
                        "cache_create_5m_tokens": _num(r[5]), "cache_create_1h_tokens": 0}
        except Exception:
            pass
    # Live fallback.
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COUNT(DISTINCT session_id) AS sessions,
             SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM dashboard.messages WHERE 1=1 {rng}
    """
    if args:
        cur.execute(sql, tuple(args))
    else:
        cur.execute(sql)
    row = cur.fetchone() or (0,) * 7
    return _drow(_OVERVIEW_KEYS, row)


def expensive_prompts(_db_path, limit: int = 50, sort: str = "tokens") -> list:
    order = "u.timestamp DESC" if sort == "recent" else "billable_tokens DESC"
    sql = f"""
      SELECT u.uuid AS user_uuid, u.session_id, u.project_slug, u.timestamp,
             u.prompt_text, u.prompt_chars,
             a.uuid AS assistant_uuid, a.model,
             COALESCE(a.input_tokens,0)+COALESCE(a.output_tokens,0)
               +COALESCE(a.cache_create_5m_tokens,0)+COALESCE(a.cache_create_1h_tokens,0) AS billable_tokens,
             COALESCE(a.cache_read_tokens,0) AS cache_read_tokens
        FROM dashboard.messages u
        JOIN dashboard.messages a ON a.parent_uuid = u.uuid AND a.type='assistant'
       WHERE u.type='user' AND u.prompt_text IS NOT NULL
       ORDER BY {order}
       LIMIT $1
    """
    cur = _conn().cursor()
    cur.execute(sql, (int(limit),))
    cols = ["user_uuid", "session_id", "project_slug", "timestamp",
            "prompt_text", "prompt_chars", "assistant_uuid", "model",
            "billable_tokens", "cache_read_tokens"]
    return [_drow(cols, r) for r in cur.fetchall()]


def project_summary(_db_path, since=None, until=None) -> list:
    cols = ["project_slug", "sessions", "turns", "input_tokens", "output_tokens",
            "billable_tokens", "cache_read_tokens"]
    cur = _conn().cursor()
    rows = None
    # All-time → MV (read all rows, sort in Python; MV COUNT(*) is buggy but
    # full SELECT is correct + fast).
    if not since and not until:
        try:
            cur.execute("SELECT project_slug, sessions, turns, input_tokens, "
                        "output_tokens, billable_tokens, cache_read_tokens "
                        "FROM td_project_summary")
            mv_rows = [_drow(cols, r) for r in cur.fetchall()]
            if mv_rows:
                mv_rows.sort(key=lambda x: -(x["billable_tokens"] or 0))
                rows = mv_rows
        except Exception:
            rows = None
    if rows is None:
        rng, args = _range_clause(since, until)
        sql = f"""
          SELECT project_slug,
                 COUNT(DISTINCT session_id) AS sessions,
                 SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
                 COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                 COALESCE(SUM(output_tokens), 0) AS output_tokens,
                 SUM(input_tokens)+SUM(output_tokens)
                   +SUM(cache_create_5m_tokens)+SUM(cache_create_1h_tokens) AS billable_tokens,
                 SUM(cache_read_tokens) AS cache_read_tokens
            FROM dashboard.messages
           WHERE 1=1 {rng}
           GROUP BY project_slug
           ORDER BY billable_tokens DESC
        """
        if args:
            cur.execute(sql, tuple(args))
        else:
            cur.execute(sql)
        rows = [_drow(cols, r) for r in cur.fetchall()]
    # Resolve pretty project names from SQLite (indexed, sub-10ms).
    if rows:
        names = _resolve_project_names(_db_path, [r["project_slug"] for r in rows])
        for r in rows:
            r["project_name"] = names.get(r["project_slug"], r["project_slug"])
    return rows


def tool_token_breakdown(_db_path, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    rng_t = rng.replace("timestamp", "t.timestamp")
    sql = f"""
      SELECT t.tool_name,
             COUNT(*) AS calls,
             COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) AS result_tokens
        FROM dashboard.tool_calls t
        LEFT JOIN dashboard.tool_results r ON r.tool_use_id = t.tool_use_id
       WHERE t.tool_name != '_tool_result' {rng_t}
       GROUP BY t.tool_name
       ORDER BY calls DESC
    """
    cur = _conn().cursor()
    if args:
        cur.execute(sql, tuple(args))
    else:
        cur.execute(sql)
    rows = []
    for tool_name, calls, result_tokens in cur.fetchall():
        calls = _num(calls)
        row = {"tool_name": tool_name, "calls": calls, "result_tokens": _num(result_tokens) or 0}
        row["is_mcp"] = tool_name.startswith("mcp__")
        row["tokens_per_call"] = (row["result_tokens"] / row["calls"]) if row["calls"] else 0
        rows.append(row)
    return rows


def recent_sessions(_db_path, limit: int = 20, since=None, until=None) -> list:
    cols = ["session_id", "project_slug", "started", "ended", "turns", "tokens"]
    cur = _conn().cursor()
    rows = None
    # All-time → MV, ordered + limited at read time.
    if not since and not until:
        try:
            cur.execute("SELECT session_id, project_slug, started, ended, turns, tokens "
                        "FROM td_session_summary ORDER BY ended DESC LIMIT $1", (int(limit),))
            rows = [_drow(cols, r) for r in cur.fetchall()]
        except Exception:
            rows = None
    if rows is None:
        rng, args = _range_clause(since, until)
        # NB: avoid CTE+param combo (v3.26 quirk) — single GROUP BY query.
        sql = f"""
          SELECT session_id, project_slug,
                 MIN(timestamp) AS started, MAX(timestamp) AS ended,
                 SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
                 SUM(input_tokens)+SUM(output_tokens) AS tokens
            FROM dashboard.messages
           WHERE 1=1 {rng}
           GROUP BY session_id, project_slug
           ORDER BY ended DESC
           LIMIT ${len(args)+1}
        """
        cur.execute(sql, tuple(args) + (int(limit),))
        rows = [_drow(cols, r) for r in cur.fetchall()]
    # Pretty names from SQLite (indexed, sub-10ms).
    if rows:
        names = _resolve_project_names(_db_path, list({r["project_slug"] for r in rows}))
        for r in rows:
            r["project_name"] = names.get(r["project_slug"], r["project_slug"])
    return rows


def session_turns(_db_path, session_id: str) -> list:
    sql = """
      SELECT uuid, parent_uuid, type, timestamp, model, 0 AS is_sidechain, NULL AS agent_id,
             input_tokens, output_tokens, cache_read_tokens,
             cache_create_5m_tokens, cache_create_1h_tokens,
             prompt_text, prompt_chars, tool_calls_json, project_slug, cwd
        FROM dashboard.messages
       WHERE session_id = $1
       ORDER BY timestamp ASC
    """
    cur = _conn().cursor()
    cur.execute(sql, (session_id,))
    cols = ["uuid", "parent_uuid", "type", "timestamp", "model", "is_sidechain",
            "agent_id", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_create_5m_tokens", "cache_create_1h_tokens", "prompt_text",
            "prompt_chars", "tool_calls_json", "project_slug", "cwd"]
    return [_drow(cols, r) for r in cur.fetchall()]


def daily_token_breakdown(_db_path, since=None, until=None) -> list:
    cols = ["day", "input_tokens", "output_tokens", "cache_read_tokens", "cache_create_tokens"]
    cur = _conn().cursor()
    # The day-grain MV serves this directly (it IS day-grain). Filter by the
    # `day` column for ranges; sort in Python (MV ORDER BY is fine but keep
    # it uniform).
    try:
        day_where, day_args = _range_clause(since, until, col="day")
        sql = f"""SELECT day, input_tokens, output_tokens, cache_read_tokens, cache_create_tokens
                    FROM td_overview_daily WHERE 1=1 {day_where}"""
        if day_args:
            cur.execute(sql, tuple(day_args))
        else:
            cur.execute(sql)
        rows = [_drow(cols, r) for r in cur.fetchall()]
        if rows:
            rows.sort(key=lambda x: x["day"] or "")
            return rows
    except Exception:
        pass
    # Live fallback.
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT SUBSTR(timestamp, 1, 10) AS day,
             COALESCE(SUM(input_tokens),0)      AS input_tokens,
             COALESCE(SUM(output_tokens),0)     AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)
               + COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_tokens
        FROM dashboard.messages
       WHERE timestamp IS NOT NULL {rng}
       GROUP BY SUBSTR(timestamp, 1, 10)
       ORDER BY SUBSTR(timestamp, 1, 10) ASC
    """
    if args:
        cur.execute(sql, tuple(args))
    else:
        cur.execute(sql)
    return [_drow(cols, r) for r in cur.fetchall()]


def skill_breakdown(_db_path, since=None, until=None) -> list:
    cols = ["skill", "invocations", "sessions", "last_used"]
    cur = _conn().cursor()
    if not since and not until:
        try:
            cur.execute("SELECT skill, invocations, sessions, last_used FROM td_skill_breakdown")
            rows = [_drow(cols, r) for r in cur.fetchall()]
            if rows:
                rows.sort(key=lambda x: -(x["invocations"] or 0))
                return rows
        except Exception:
            pass
    rng, args = _range_clause(since, until)
    rng_t = rng.replace("timestamp", "tc.timestamp")
    sql = f"""
      SELECT tc.target AS skill,
             COUNT(*) AS invocations,
             COUNT(DISTINCT tc.session_id) AS sessions,
             MAX(tc.timestamp) AS last_used
        FROM dashboard.tool_calls tc
       WHERE tc.tool_name = 'Skill' AND tc.target IS NOT NULL AND tc.target != '' {rng_t}
       GROUP BY tc.target
       ORDER BY invocations DESC
    """
    if args:
        cur.execute(sql, tuple(args))
    else:
        cur.execute(sql)
    return [_drow(cols, r) for r in cur.fetchall()]


def model_breakdown(_db_path, since=None, until=None) -> list:
    cols = ["model", "turns", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_create_5m_tokens", "cache_create_1h_tokens"]
    cur = _conn().cursor()
    if not since and not until:
        try:
            cur.execute("SELECT model, turns, input_tokens, output_tokens, "
                        "cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens "
                        "FROM td_model_breakdown")
            rows = [_drow(cols, r) for r in cur.fetchall()]
            if rows:
                rows.sort(key=lambda x: -((x["input_tokens"] or 0) + (x["output_tokens"] or 0)
                                          + (x["cache_create_5m_tokens"] or 0) + (x["cache_create_1h_tokens"] or 0)))
                return rows
        except Exception:
            pass
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COALESCE(model, 'unknown') AS model,
             COUNT(*) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM dashboard.messages
       WHERE type = 'assistant' {rng}
       GROUP BY model
       ORDER BY (input_tokens + output_tokens + cache_create_5m_tokens + cache_create_1h_tokens) DESC
    """
    if args:
        cur.execute(sql, tuple(args))
    else:
        cur.execute(sql)
    return [_drow(cols, r) for r in cur.fetchall()]


# ---------- MCP / savings rollups (mirror the db.py signatures) ----------

def mcp_summary(_db_path, since=None, until=None) -> dict:
    rng, args = _range_clause(since, until)
    rng_t = rng.replace("timestamp", "t.timestamp")
    sql = f"""
      SELECT
        COUNT(*) AS mcp_calls,
        COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) AS mcp_tokens,
        COALESCE(SUM(t.baseline_tokens), 0) AS baseline_tokens,
        COALESCE(SUM(
          CASE WHEN t.baseline_tokens IS NOT NULL
                AND t.baseline_tokens > COALESCE(r.result_tokens, t.result_tokens, 0)
               THEN t.baseline_tokens - COALESCE(r.result_tokens, t.result_tokens, 0)
               ELSE 0 END
        ), 0) AS savings_tokens_per_call,
        SUM(CASE WHEN t.baseline_method='estimated' THEN 1 ELSE 0 END) AS estimated,
        SUM(CASE WHEN t.baseline_method='measured'  THEN 1 ELSE 0 END) AS measured,
        SUM(CASE WHEN t.baseline_method='tracking_only' THEN 1 ELSE 0 END) AS tracking_only,
        SUM(CASE WHEN t.baseline_method IS NULL THEN 1 ELSE 0 END) AS pending,
        SUM(CASE WHEN t.resolution='exact'      THEN 1 ELSE 0 END) AS exact_res,
        SUM(CASE WHEN t.resolution='heuristic'  THEN 1 ELSE 0 END) AS heuristic_res,
        SUM(CASE WHEN t.resolution='unresolved' THEN 1 ELSE 0 END) AS unresolved_res,
        0 AS with_followup
      FROM dashboard.tool_calls t
      LEFT JOIN dashboard.tool_results r ON r.tool_use_id = t.tool_use_id
      WHERE t.tool_name LIKE 'mcp__%' {rng_t}
    """
    cur = _conn().cursor()
    if args:
        cur.execute(sql, tuple(args))
    else:
        cur.execute(sql)
    keys = ["mcp_calls", "mcp_tokens", "baseline_tokens", "savings_tokens_per_call",
            "estimated", "measured", "tracking_only", "pending",
            "exact_res", "heuristic_res", "unresolved_res", "with_followup"]
    out = dict(zip(keys, cur.fetchone() or (0,) * len(keys)))
    # All-tool tokens for share calc
    cur2 = _conn().cursor()
    if args:
        cur2.execute(
            f"SELECT COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) "
            f"FROM dashboard.tool_calls t "
            f"LEFT JOIN dashboard.tool_results r ON r.tool_use_id = t.tool_use_id "
            f"WHERE t.tool_name != '_tool_result' {rng_t}", tuple(args))
    else:
        cur2.execute(
            f"SELECT COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) "
            f"FROM dashboard.tool_calls t "
            f"LEFT JOIN dashboard.tool_results r ON r.tool_use_id = t.tool_use_id "
            f"WHERE t.tool_name != '_tool_result'")
    out["all_tool_tokens"] = (cur2.fetchone() or (0,))[0] or 0
    out["mcp_token_share"] = round(out["mcp_tokens"] / out["all_tool_tokens"], 4) if out["all_tool_tokens"] else 0.0
    total_resolved = (out.get("exact_res") or 0) + (out.get("heuristic_res") or 0) + (out.get("unresolved_res") or 0)
    out["exact_resolution_rate"] = round((out.get("exact_res") or 0) / total_resolved, 4) if total_resolved else None
    out["followup_rate"] = 0.0
    out["savings_tokens"] = out.pop("savings_tokens_per_call", 0) or 0
    out["savings_ratio"] = round(out["savings_tokens"] / out["baseline_tokens"], 4) if out["baseline_tokens"] else 0.0
    return out


def mcp_per_tool(_db_path, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    rng_t = rng.replace("timestamp", "t.timestamp")
    sql = f"""
      SELECT t.tool_name,
             COUNT(*) AS calls,
             COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) AS result_tokens,
             COALESCE(SUM(t.baseline_tokens), 0) AS baseline_tokens,
             SUM(CASE WHEN t.baseline_method IS NOT NULL AND t.baseline_method != 'tracking_only' THEN 1 ELSE 0 END) AS baseline_count,
             0 AS followup_calls,
             SUM(CASE WHEN t.resolution='exact' THEN 1 ELSE 0 END) AS exact_res,
             SUM(CASE WHEN t.resolution IS NOT NULL THEN 1 ELSE 0 END) AS resolved
        FROM dashboard.tool_calls t
        LEFT JOIN dashboard.tool_results r ON r.tool_use_id = t.tool_use_id
       WHERE t.tool_name LIKE 'mcp__%' {rng_t}
       GROUP BY t.tool_name
       ORDER BY calls DESC
    """
    cur = _conn().cursor()
    if args:
        cur.execute(sql, tuple(args))
    else:
        cur.execute(sql)
    cols = ["tool_name", "calls", "result_tokens", "baseline_tokens",
            "baseline_count", "followup_calls", "exact_res", "resolved"]
    rows = [_drow(cols, r) for r in cur.fetchall()]
    for row in rows:
        row["tokens_per_call"] = (row["result_tokens"] / row["calls"]) if row["calls"] else 0
        row["savings_tokens"] = max(0, (row["baseline_tokens"] or 0) - (row["result_tokens"] or 0))
        row["savings_ratio"] = (row["savings_tokens"] / row["baseline_tokens"]) if row["baseline_tokens"] else 0
        row["followup_rate"] = 0
        row["exact_rate"] = (row["exact_res"] / row["resolved"]) if row["resolved"] else None
    return rows


def mcp_recent_calls(_db_path, limit: int = 50) -> list:
    sql = """
      SELECT t.id, t.tool_name, t.target, t.tool_use_id, t.session_id, t.project_slug,
             t.timestamp, COALESCE(r.result_tokens, t.result_tokens) AS result_tokens,
             t.baseline_tokens, t.baseline_method,
             t.resolution, 0 AS followup_within_turn,
             mr.response_tokens AS replay_tokens, mr.replayed_at, mr.took_ms, mr.ok AS replay_ok
        FROM dashboard.tool_calls t
        LEFT JOIN dashboard.tool_results r ON r.tool_use_id = t.tool_use_id
        LEFT JOIN dashboard.mcp_replay mr ON mr.call_id = t.id
       WHERE t.tool_name LIKE 'mcp__%'
       ORDER BY t.timestamp DESC
       LIMIT $1
    """
    cur = _conn().cursor()
    cur.execute(sql, (int(limit),))
    cols = ["id", "tool_name", "target", "tool_use_id", "session_id", "project_slug",
            "timestamp", "result_tokens", "baseline_tokens", "baseline_method",
            "resolution", "followup_within_turn", "replay_tokens", "replayed_at",
            "took_ms", "replay_ok"]
    rows = [_drow(cols, r) for r in cur.fetchall()]
    for row in rows:
        if row.get("baseline_tokens") and row.get("result_tokens") is not None:
            row["savings_tokens"] = max(0, row["baseline_tokens"] - row["result_tokens"])
        else:
            row["savings_tokens"] = None
    return rows


def mcp_top_callers(_db_path, since=None, until=None, limit: int = 10) -> dict:
    rng, args = _range_clause(since, until)
    rng_t = rng.replace("timestamp", "t.timestamp")
    cur = _conn().cursor()
    cur.execute(f"""
      SELECT t.project_slug, COUNT(*) AS mcp_calls,
             COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) AS mcp_tokens,
             COALESCE(SUM(t.baseline_tokens), 0) AS baseline_tokens
        FROM dashboard.tool_calls t
        LEFT JOIN dashboard.tool_results r ON r.tool_use_id = t.tool_use_id
       WHERE t.tool_name LIKE 'mcp__%' {rng_t}
       GROUP BY t.project_slug
       ORDER BY mcp_calls DESC
       LIMIT ${len(args)+1}
    """, tuple(args) + (int(limit),))
    proj = [{"project_slug": r[0], "mcp_calls": r[1], "mcp_tokens": r[2],
             "baseline_tokens": r[3]} for r in cur.fetchall()]
    cur2 = _conn().cursor()
    cur2.execute(f"""
      SELECT t.session_id, t.project_slug, COUNT(*) AS mcp_calls,
             COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) AS mcp_tokens,
             COALESCE(SUM(t.baseline_tokens), 0) AS baseline_tokens,
             MAX(t.timestamp) AS last_call
        FROM dashboard.tool_calls t
        LEFT JOIN dashboard.tool_results r ON r.tool_use_id = t.tool_use_id
       WHERE t.tool_name LIKE 'mcp__%' {rng_t}
       GROUP BY t.session_id, t.project_slug
       ORDER BY mcp_calls DESC
       LIMIT ${len(args)+1}
    """, tuple(args) + (int(limit),))
    sess = [{"session_id": r[0], "project_slug": r[1], "mcp_calls": r[2],
             "mcp_tokens": r[3], "baseline_tokens": r[4], "last_call": r[5]}
            for r in cur2.fetchall()]
    return {"projects": proj, "sessions": sess}


def project_mcp_share(_db_path, since=None, until=None) -> dict:
    rng, args = _range_clause(since, until)
    rng_t = rng.replace("timestamp", "t.timestamp")
    cur = _conn().cursor()
    sql = f"""
      SELECT t.project_slug, COUNT(*) AS mcp_calls,
             COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) AS mcp_tokens,
             COALESCE(SUM(t.baseline_tokens), 0) AS baseline_tokens
        FROM dashboard.tool_calls t
        LEFT JOIN dashboard.tool_results r ON r.tool_use_id = t.tool_use_id
       WHERE t.tool_name LIKE 'mcp__%' {rng_t}
       GROUP BY t.project_slug
    """
    if args:
        cur.execute(sql, tuple(args))
    else:
        cur.execute(sql)
    return {r[0]: {"project_slug": r[0], "mcp_calls": r[1], "mcp_tokens": r[2],
                   "baseline_tokens": r[3]} for r in cur.fetchall()}


def session_mcp_share(_db_path, since=None, until=None) -> dict:
    rng, args = _range_clause(since, until)
    rng_t = rng.replace("timestamp", "t.timestamp")
    cur = _conn().cursor()
    sql = f"""
      SELECT t.session_id, COUNT(*) AS mcp_calls,
             COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) AS mcp_tokens,
             COALESCE(SUM(t.baseline_tokens), 0) AS baseline_tokens
        FROM dashboard.tool_calls t
        LEFT JOIN dashboard.tool_results r ON r.tool_use_id = t.tool_use_id
       WHERE t.tool_name LIKE 'mcp__%' {rng_t}
       GROUP BY t.session_id
    """
    if args:
        cur.execute(sql, tuple(args))
    else:
        cur.execute(sql)
    return {r[0]: {"session_id": r[0], "mcp_calls": r[1], "mcp_tokens": r[2],
                   "baseline_tokens": r[3]} for r in cur.fetchall()}


def prompt_mcp_share(_db_path, user_uuids: list) -> dict:
    if not user_uuids:
        return {}
    in_clause = ",".join(f"${i+1}" for i in range(len(user_uuids)))
    sql = f"""
      SELECT u.uuid AS user_uuid,
             COUNT(t.id) AS mcp_calls,
             COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) AS mcp_tokens,
             COALESCE(SUM(t.baseline_tokens), 0) AS baseline_tokens
        FROM dashboard.messages u
        JOIN dashboard.messages a ON a.parent_uuid = u.uuid AND a.type='assistant'
        LEFT JOIN dashboard.tool_calls t ON t.message_uuid = a.uuid
                                        AND t.tool_name LIKE 'mcp__%'
        LEFT JOIN dashboard.tool_results r ON r.tool_use_id = t.tool_use_id
       WHERE u.uuid IN ({in_clause})
       GROUP BY u.uuid
    """
    cur = _conn().cursor()
    cur.execute(sql, tuple(user_uuids))
    return {r[0]: {"user_uuid": r[0], "mcp_calls": r[1], "mcp_tokens": r[2],
                   "baseline_tokens": r[3]} for r in cur.fetchall()}


# ---------- join-heavy routes: delegate to the SQLite path ----------
# expensive_prompts (user→assistant self-join) and the mcp_* rollups
# (tool_calls ⨝ tool_results) can't ride a materialized view — HeliosDB
# v3.33.0 JOIN-based MVs read inconsistently, and the live joins are
# 6-60s on the 450k-row mirror. SQLite serves all of these in <1s and
# already holds the full tool-call data (baseline_tokens, resolution,
# followup_within_turn), so we delegate just these functions back to it.
# Everything else above stays HeliosDB-MV-backed. Tracked for removal
# once HeliosDB ships denormalized result_tokens or correct JOIN-MVs.
from .db import (                                       # noqa: E402
    expensive_prompts,
    mcp_summary,
    mcp_per_tool,
    mcp_recent_calls,
    mcp_top_callers,
    project_mcp_share,
    session_mcp_share,
    prompt_mcp_share,
)
