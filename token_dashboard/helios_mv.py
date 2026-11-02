"""Materialized views for the HeliosDB-as-primary cutover.

The dashboard's slow rollup queries (project_summary, recent_sessions,
overview, tool/model/skill breakdowns, mcp_per_tool) are GROUP BY
aggregates over the full 450k-row `dashboard.messages` / `dashboard.tool_*`
tables — 0.5–18 s each on HeliosDB v3.33.0 via live SQL. Materialized as
views they collapse to single-table reads that return in <5 ms.

Requires HeliosDB ≥ v3.32.2 — earlier versions materialized wrong
aggregates at scale (issue #2 / Quirk J). Verified on v3.33.0.

Two v3.33.0 MV caveats this module works around:
  - `COUNT(*)` over an MV returns 0 (data reads are fine; bare COUNT is
    broken). We never COUNT(*) an MV — `db_helios` reads them with
    `SELECT cols … [ORDER BY … LIMIT]`, and uses `SUM(1)` if a count is
    ever needed.
  - JOIN-based MVs read inconsistently (self-join MV returned rows via
    ORDER BY but 0 via COUNT). So every MV here is single-table GROUP BY
    only — the `expensive_prompts` self-join stays a live query.

MVs are all-time aggregates. `db_helios` reads them directly for the
no-range (all-time) path — which is what the Projects / Sessions /
Prompts / Skills tabs use — and falls back to live SQL for the ranged
Overview / MCP queries (`td_overview_daily` keeps those fast too).
"""
from __future__ import annotations

import time

from . import helios_writer as hw


# name → CREATE MATERIALIZED VIEW body (single-table GROUP BY only)
MV_DEFS: dict[str, str] = {
    # /api/overview — all-time single row (exact; no-range path)
    "td_overview": """
        SELECT COUNT(DISTINCT session_id) AS sessions,
               SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
               COALESCE(SUM(input_tokens),0)            AS input_tokens,
               COALESCE(SUM(output_tokens),0)           AS output_tokens,
               COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
               COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
               COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
          FROM dashboard.messages
    """,
    # /api/daily + ranged overview — per-day additive grain
    "td_overview_daily": """
        SELECT SUBSTR(timestamp, 1, 10) AS day,
               COUNT(DISTINCT session_id) AS sessions,
               SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
               COALESCE(SUM(input_tokens),0)      AS input_tokens,
               COALESCE(SUM(output_tokens),0)     AS output_tokens,
               COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
               COALESCE(SUM(cache_create_5m_tokens),0)
                 + COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_tokens
          FROM dashboard.messages
         WHERE timestamp IS NOT NULL
         GROUP BY SUBSTR(timestamp, 1, 10)
    """,
    # /api/projects — per-project all-time
    "td_project_summary": """
        SELECT project_slug,
               COUNT(DISTINCT session_id) AS sessions,
               SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
               COALESCE(SUM(input_tokens), 0)  AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               SUM(input_tokens)+SUM(output_tokens)
                 +SUM(cache_create_5m_tokens)+SUM(cache_create_1h_tokens) AS billable_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens
          FROM dashboard.messages
         GROUP BY project_slug
    """,
    # /api/sessions — per-session all-time
    "td_session_summary": """
        SELECT session_id, project_slug,
               MIN(timestamp) AS started, MAX(timestamp) AS ended,
               SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
               SUM(input_tokens)+SUM(output_tokens) AS tokens
          FROM dashboard.messages
         GROUP BY session_id, project_slug
    """,
    # /api/by-model — per-model all-time (assistant turns)
    "td_model_breakdown": """
        SELECT COALESCE(model, 'unknown') AS model,
               COUNT(*) AS turns,
               COALESCE(SUM(input_tokens),0)            AS input_tokens,
               COALESCE(SUM(output_tokens),0)           AS output_tokens,
               COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
               COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
               COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
          FROM dashboard.messages
         WHERE type = 'assistant'
         GROUP BY model
    """,
    # NB: a `td_project_cwds` distinct-pairs MV was tried for name
    # resolution but HeliosDB v3.33.0 doesn't dedupe a GROUP-BY-without-
    # aggregate MV (it materialized all 453k rows). Name resolution is done
    # from SQLite's indexed table instead — see db_helios._resolve_project_names.
    # /api/skills — per-skill all-time
    "td_skill_breakdown": """
        SELECT target AS skill,
               COUNT(*) AS invocations,
               COUNT(DISTINCT session_id) AS sessions,
               MAX(timestamp) AS last_used
          FROM dashboard.tool_calls
         WHERE tool_name = 'Skill' AND target IS NOT NULL AND target != ''
         GROUP BY target
    """,
}

# Tables an MV depends on — used to decide whether a refresh is worthwhile.
MV_BASE_TABLES: dict[str, str] = {
    "td_overview": "dashboard.messages",
    "td_overview_daily": "dashboard.messages",
    "td_project_summary": "dashboard.messages",
    "td_session_summary": "dashboard.messages",
    "td_model_breakdown": "dashboard.messages",
    "td_skill_breakdown": "dashboard.tool_calls",
}


def ensure_mvs(conn=None) -> dict:
    """Create any missing MVs. Idempotent (CREATE … then ignore 'exists')."""
    conn = conn or hw.get_conn()
    if conn is None:
        return {"ok": False, "error": "helios not configured"}
    cur = conn.cursor()
    created, existed = [], []
    for name, body in MV_DEFS.items():
        try:
            cur.execute(f"CREATE MATERIALIZED VIEW {name} AS {body}")
            hw._commit(conn)
            created.append(name)
        except Exception as e:
            msg = str(e).lower()
            if "exist" in msg or "already" in msg:
                existed.append(name)
            else:
                # Surface unexpected DDL errors but keep going.
                created.append(f"{name}:ERR:{str(e)[:80]}")
    return {"ok": True, "created": created, "existed": existed}


def refresh_all(conn=None, incremental: bool = True) -> dict:
    """REFRESH every MV. Incremental when supported; full otherwise.

    Called from the scan loop after each ingest and via /api/helios/refresh-mvs.
    """
    conn = conn or hw.get_conn()
    if conn is None:
        return {"ok": False, "error": "helios not configured"}
    cur = conn.cursor()
    started = time.time()
    refreshed = {}
    for name in MV_DEFS:
        t0 = time.time()
        ok = False
        # Try incremental first, fall back to full.
        for stmt in ([f"REFRESH MATERIALIZED VIEW {name} INCREMENTALLY",
                      f"REFRESH MATERIALIZED VIEW {name}"] if incremental
                     else [f"REFRESH MATERIALIZED VIEW {name}"]):
            try:
                cur.execute(stmt)
                hw._commit(conn)
                ok = True
                break
            except Exception:
                continue
        refreshed[name] = {"ok": ok, "ms": int((time.time() - t0) * 1000)}
    return {"ok": True, "elapsed_ms": int((time.time() - started) * 1000),
            "views": refreshed}


def drop_all(conn=None) -> dict:
    conn = conn or hw.get_conn()
    if conn is None:
        return {"ok": False, "error": "helios not configured"}
    cur = conn.cursor()
    for name in MV_DEFS:
        try:
            cur.execute(f"DROP MATERIALIZED VIEW IF EXISTS {name}")
            hw._commit(conn)
        except Exception:
            pass
    return {"ok": True}


def staleness(conn=None) -> list:
    """pg_mv_staleness() rows for our MVs (the \\dmv surface)."""
    conn = conn or hw.get_conn()
    if conn is None:
        return []
    cur = conn.cursor()
    try:
        cur.execute("SELECT view_name, last_update, pending_changes, staleness_sec, status FROM pg_mv_staleness()")
        out = []
        for r in cur.fetchall():
            if r[0] in MV_DEFS:
                out.append({"view_name": r[0], "last_update": str(r[1]),
                            "pending_changes": r[2], "staleness_sec": r[3],
                            "status": r[4]})
        return out
    except Exception as e:
        return [{"error": str(e)[:160]}]
