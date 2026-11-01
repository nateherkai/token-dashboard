"""SQLite schema, connection, and shared query helpers.

Backend selection (env-driven, decided once at import):

* ``TOKEN_DASHBOARD_BACKEND=sqlite`` — force the stdlib ``sqlite3`` module
  (legacy / dev / CI without HeliosDB available).
* ``TOKEN_DASHBOARD_BACKEND=heliosdb`` — force the ``heliosdb_sqlite`` shim.
* ``TOKEN_DASHBOARD_BACKEND=auto`` (default) — prefer ``heliosdb_sqlite``
  if it is installed; fall back to stdlib ``sqlite3``.

When using HeliosDB:

* ``HELIOSDB_DSN`` set → daemon mode (psycopg2 → running ``heliosdb-nano start``
  on PG wire). Faster per-query latency; expects a server in the background.
* ``HELIOSDB_DSN`` unset → embedded mode (spawns ``heliosdb-nano repl`` per
  connection). Zero-config, single-file ergonomics; slower per-query.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union

# ----------------------------------------------------------------------
# Backend resolution
# ----------------------------------------------------------------------
_BACKEND_PREF = os.environ.get("TOKEN_DASHBOARD_BACKEND", "auto").lower()
_HELIOSDB_DSN = os.environ.get("HELIOSDB_DSN")

if _BACKEND_PREF == "sqlite":
    import sqlite3  # type: ignore[import]
    _USING_HELIOSDB = False
elif _BACKEND_PREF == "heliosdb":
    import heliosdb_sqlite as sqlite3  # type: ignore[no-redef]
    _USING_HELIOSDB = True
else:  # "auto"
    try:
        import heliosdb_sqlite as sqlite3  # type: ignore[no-redef]
        _USING_HELIOSDB = True
    except ImportError:
        import sqlite3  # type: ignore[import]
        _USING_HELIOSDB = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path        TEXT PRIMARY KEY,
  mtime       REAL    NOT NULL,
  bytes_read  INTEGER NOT NULL,
  scanned_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  uuid                    TEXT PRIMARY KEY,
  parent_uuid             TEXT,
  session_id              TEXT NOT NULL,
  project_slug            TEXT NOT NULL,
  cwd                     TEXT,
  git_branch              TEXT,
  cc_version              TEXT,
  entrypoint              TEXT,
  type                    TEXT NOT NULL,
  is_sidechain            INTEGER NOT NULL DEFAULT 0,
  agent_id                TEXT,
  timestamp               TEXT NOT NULL,
  model                   TEXT,
  stop_reason             TEXT,
  prompt_id               TEXT,
  message_id              TEXT,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens  INTEGER NOT NULL DEFAULT 0,
  prompt_text             TEXT,
  prompt_chars            INTEGER,
  tool_calls_json         TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_project   ON messages(project_slug);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_model     ON messages(model);
CREATE INDEX IF NOT EXISTS idx_messages_msgid     ON messages(session_id, message_id);

CREATE TABLE IF NOT EXISTS tool_calls (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  message_uuid          TEXT    NOT NULL,
  session_id            TEXT    NOT NULL,
  project_slug          TEXT    NOT NULL,
  tool_name             TEXT    NOT NULL,
  target                TEXT,
  tool_use_id           TEXT,
  result_tokens         INTEGER,
  is_error              INTEGER NOT NULL DEFAULT 0,
  timestamp             TEXT    NOT NULL,
  baseline_tokens       INTEGER,
  baseline_method       TEXT,
  resolution            TEXT,
  followup_within_turn  INTEGER,
  mcp_meta_json         TEXT
);
CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tools_name    ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tools_target  ON tool_calls(target);
CREATE INDEX IF NOT EXISTS idx_tools_msguuid ON tool_calls(message_uuid);
CREATE INDEX IF NOT EXISTS idx_tools_project ON tool_calls(project_slug);
CREATE INDEX IF NOT EXISTS idx_tools_useid   ON tool_calls(tool_use_id);

CREATE TABLE IF NOT EXISTS mcp_replay (
  call_id           INTEGER PRIMARY KEY,
  tool_name         TEXT    NOT NULL,
  replayed_at       REAL    NOT NULL,
  response_tokens   INTEGER,
  took_ms           INTEGER,
  ok                INTEGER NOT NULL,
  detail            TEXT,
  FOREIGN KEY (call_id) REFERENCES tool_calls(id)
);

CREATE TABLE IF NOT EXISTS mcp_state (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS plan (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS dismissed_tips (
  tip_key       TEXT PRIMARY KEY,
  dismissed_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS project_budgets (
  project_slug TEXT PRIMARY KEY,
  daily_usd REAL,
  monthly_usd REAL,
  alert_webhook TEXT,
  updated_at REAL NOT NULL
);
"""


def default_db_path() -> Path:
    return Path.home() / ".claude" / "token-dashboard.db"


def init_db(path: Union[str, Path]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use the shared connect() helper so backend / daemon-mode resolution
    # is consistent across init and runtime queries.
    with connect(path) as c:
        _migrate_add_message_id(c)
        _migrate_add_tool_columns(c)
        c.executescript(SCHEMA)


def _migrate_add_message_id(conn) -> None:
    """Add messages.message_id for streaming-snapshot dedup.

    Why: pre-migration rows were summed from all streaming snapshots (over-count).
    How to apply: if the old table exists without the column, add it and clear
    messages/tool_calls/files so the next scan replays JSONLs cleanly. Source
    of truth is on disk; rescanning is cheap.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "message_id" in cols:
        return
    conn.execute("ALTER TABLE messages ADD COLUMN message_id TEXT")
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM tool_calls")
    conn.execute("DELETE FROM files")
    conn.commit()


def _migrate_add_tool_columns(conn) -> None:
    """Add savings/quality columns to tool_calls. Non-destructive (no data loss).

    Backfills tool_use_id on existing `_tool_result` rows from `target` (where
    the value already lives). Existing tool_use rows from pre-migration scans
    can't recover their tool_use_id without re-reading the JSONL — they get
    attributed only on the next scan that re-reads them. New scans capture
    tool_use_id natively for both shapes.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_calls'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
    additions = [
        ("tool_use_id",          "TEXT"),
        ("baseline_tokens",      "INTEGER"),
        ("baseline_method",      "TEXT"),
        ("resolution",           "TEXT"),
        ("followup_within_turn", "INTEGER"),
        ("mcp_meta_json",        "TEXT"),
    ]
    changed = False
    for name, ddl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE tool_calls ADD COLUMN {name} {ddl}")
            changed = True
    if changed:
        # _tool_result rows already store the tool_use_id in `target` —
        # populate the new column from there in one statement.
        conn.execute(
            "UPDATE tool_calls SET tool_use_id = target "
            "WHERE tool_name='_tool_result' AND tool_use_id IS NULL AND target IS NOT NULL"
        )
        conn.commit()


@contextmanager
def connect(path: Union[str, Path]):
    if _USING_HELIOSDB and _HELIOSDB_DSN:
        # Daemon mode: psycopg2 → running heliosdb-nano server.
        conn = sqlite3.connect(
            str(path),
            mode="daemon",
            dsn=_HELIOSDB_DSN,
            timeout=30.0,
        )
    else:
        # Embedded mode (HeliosDB) or stdlib sqlite3 — same call signature.
        conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # PRAGMAs are advisory under HeliosDB (no-op for journal_mode/synchronous/
    # busy_timeout; foreign_keys are always on). Sending them anyway keeps
    # the code uniform across backends.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _range_clause(since, until, col: str = "timestamp"):
    where, args = [], []
    if since:
        where.append(f"{col} >= ?"); args.append(since)
    if until:
        where.append(f"{col} < ?"); args.append(until)
    return ((" AND " + " AND ".join(where)) if where else "", args)


def _encode_slug(path: str) -> str:
    """Claude Code's project-slug encoding: each of `:`, `\\`, `/`, space → one `-`."""
    return re.sub(r"[:\\/ ]", "-", path)


def _walk_to_root(cwd: str, slug: str) -> Optional[str]:
    """If any ancestor of cwd encodes to slug, return that ancestor's basename."""
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
    """Pretty project name from a single cwd + slug (best-effort).

    For the multi-cwd case, prefer `best_project_name`.
    """
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
    """Pick a pretty name from a list of cwds.

    Prefer a cwd whose walk-up matches `slug` (a true descendant of the project
    root). If none match, fall back to `project_name_for` on the first cwd,
    then to the slug's last segment.
    """
    cwds = [c for c in (cwds or []) if c]
    for cwd in cwds:
        name = _walk_to_root(cwd, slug)
        if name:
            return name
    return project_name_for(cwds[0] if cwds else None, slug)


def overview_totals(db_path, since=None, until=None) -> dict:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COUNT(DISTINCT session_id) AS sessions,
             SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages WHERE 1=1 {rng}
    """
    with connect(db_path) as c:
        return dict(c.execute(sql, args).fetchone())


def expensive_prompts(db_path, limit: int = 50, sort: str = "tokens") -> list:
    """User prompt joined with the immediately-following assistant turn's tokens.

    sort="tokens" (default) → largest billable first.
    sort="recent"           → newest first.
    """
    order = "u.timestamp DESC" if sort == "recent" else "billable_tokens DESC"
    sql = f"""
      SELECT u.uuid AS user_uuid, u.session_id, u.project_slug, u.timestamp,
             u.prompt_text, u.prompt_chars,
             a.uuid AS assistant_uuid, a.model,
             COALESCE(a.input_tokens,0)+COALESCE(a.output_tokens,0)
               +COALESCE(a.cache_create_5m_tokens,0)+COALESCE(a.cache_create_1h_tokens,0) AS billable_tokens,
             COALESCE(a.cache_read_tokens,0) AS cache_read_tokens
        FROM messages u
        JOIN messages a ON a.parent_uuid = u.uuid AND a.type='assistant'
       WHERE u.type='user' AND u.prompt_text IS NOT NULL
       ORDER BY {order}
       LIMIT ?
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (limit,))]


def project_summary(db_path, since=None, until=None) -> list:
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
        FROM messages m
       WHERE 1=1 {rng}
       GROUP BY project_slug
       ORDER BY billable_tokens DESC
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            cwds = [row["cwd"] for row in c.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                (r["project_slug"],),
            )]
            r["project_name"] = best_project_name(cwds, r["project_slug"])
    return rows


def tool_token_breakdown(db_path, since=None, until=None) -> list:
    """Per-tool stats: call count + total response tokens (from the paired
    `_tool_result` row, joined on tool_use_id) + is_mcp + tokens_per_call.

    The tool_use and tool_result blocks live in adjacent messages, so the
    pairing is by tool_use_id (set on both ends by the scanner). Rows from
    pre-tool_use_id scans get treated as 0 tokens; once the scanner re-reads
    those JSONLs, the proper pairing fills in.
    """
    rng, args = _range_clause(since, until)
    sql = f"""
      WITH calls AS (
        SELECT tool_name, tool_use_id, message_uuid, target, result_tokens, timestamp
          FROM tool_calls
         WHERE tool_name != '_tool_result' {rng}
      )
      SELECT c.tool_name,
             COUNT(*) AS calls,
             COALESCE(SUM(COALESCE(r.result_tokens, c.result_tokens, 0)), 0) AS result_tokens
        FROM calls c
        LEFT JOIN tool_calls r
               ON r.tool_use_id = c.tool_use_id
              AND r.tool_name = '_tool_result'
              AND c.tool_use_id IS NOT NULL
       GROUP BY c.tool_name
       ORDER BY calls DESC
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            r["is_mcp"] = r["tool_name"].startswith("mcp__")
            r["tokens_per_call"] = (r["result_tokens"] / r["calls"]) if r["calls"] else 0
        return rows


def mcp_summary(db_path, since=None, until=None) -> dict:
    """Headline MCP metrics: total MCP calls, total observed tokens,
    total estimated baseline, savings (per-call sum of positives), share of
    all tool tokens."""
    rng, args = _range_clause(since, until)
    with connect(db_path) as c:
        row = c.execute(f"""
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
            SUM(CASE WHEN t.followup_within_turn > 0 THEN 1 ELSE 0 END) AS with_followup
          FROM tool_calls t
          LEFT JOIN tool_calls r
                 ON r.tool_use_id = t.tool_use_id
                AND r.tool_name = '_tool_result'
                AND t.tool_use_id IS NOT NULL
          WHERE t.tool_name LIKE 'mcp__%' {rng.replace('timestamp', 't.timestamp')}
        """, args).fetchone()
        out = dict(row) if row else {}
        # Total tokens across all non-result rows in the same range
        all_t = c.execute(f"""
          SELECT COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) AS tot
            FROM tool_calls t
            LEFT JOIN tool_calls r
                   ON r.tool_use_id = t.tool_use_id
                  AND r.tool_name = '_tool_result'
                  AND t.tool_use_id IS NOT NULL
           WHERE t.tool_name != '_tool_result' {rng.replace('timestamp', 't.timestamp')}
        """, args).fetchone()
        out["all_tool_tokens"] = (all_t["tot"] if all_t else 0) or 0
        # share of tool tokens that came from MCP
        if out["all_tool_tokens"]:
            out["mcp_token_share"] = round((out["mcp_tokens"] or 0) / out["all_tool_tokens"], 4)
        else:
            out["mcp_token_share"] = 0.0
        # Quality score: fraction of LSP-shaped calls that resolved exact
        total_resolved = (out.get("exact_res") or 0) + (out.get("heuristic_res") or 0) + (out.get("unresolved_res") or 0)
        out["exact_resolution_rate"] = round((out.get("exact_res") or 0) / total_resolved, 4) if total_resolved else None
        # Followup-call rate: proxy for "MCP return wasn't enough"
        out["followup_rate"] = round((out.get("with_followup") or 0) / out["mcp_calls"], 4) if out.get("mcp_calls") else 0.0
        # Per-call positive-savings sum (avoids cancelling wins against losses).
        out["savings_tokens"] = out.pop("savings_tokens_per_call", 0) or 0
        baseline = out.get("baseline_tokens") or 0
        out["savings_ratio"] = round(out["savings_tokens"] / baseline, 4) if baseline else 0.0
        return out


def mcp_per_tool(db_path, since=None, until=None) -> list:
    """Per-MCP-tool breakdown with savings columns."""
    rng, args = _range_clause(since, until)
    with connect(db_path) as c:
        sql = f"""
          SELECT t.tool_name,
                 COUNT(*) AS calls,
                 COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)), 0) AS result_tokens,
                 COALESCE(SUM(t.baseline_tokens), 0) AS baseline_tokens,
                 SUM(CASE WHEN t.baseline_method IS NOT NULL AND t.baseline_method != 'tracking_only' THEN 1 ELSE 0 END) AS baseline_count,
                 SUM(CASE WHEN t.followup_within_turn > 0 THEN 1 ELSE 0 END) AS followup_calls,
                 SUM(CASE WHEN t.resolution='exact' THEN 1 ELSE 0 END) AS exact_res,
                 SUM(CASE WHEN t.resolution IS NOT NULL THEN 1 ELSE 0 END) AS resolved
            FROM tool_calls t
            LEFT JOIN tool_calls r
                   ON r.tool_use_id = t.tool_use_id
                  AND r.tool_name = '_tool_result'
                  AND t.tool_use_id IS NOT NULL
           WHERE t.tool_name LIKE 'mcp__%' {rng.replace('timestamp', 't.timestamp')}
           GROUP BY t.tool_name
           ORDER BY calls DESC
        """
        rows = [dict(r) for r in c.execute(sql, args)]
        for row in rows:
            row["tokens_per_call"] = (row["result_tokens"] / row["calls"]) if row["calls"] else 0
            row["savings_tokens"] = max(0, (row["baseline_tokens"] or 0) - (row["result_tokens"] or 0))
            row["savings_ratio"] = (row["savings_tokens"] / row["baseline_tokens"]) if row["baseline_tokens"] else 0
            row["followup_rate"] = (row["followup_calls"] / row["calls"]) if row["calls"] else 0
            row["exact_rate"]    = (row["exact_res"] / row["resolved"]) if row["resolved"] else None
        return rows


def mcp_recent_calls(db_path, limit: int = 50) -> list:
    """Most recent MCP tool calls with savings columns + linked replay."""
    with connect(db_path) as c:
        sql = """
          SELECT t.id, t.tool_name, t.target, t.tool_use_id, t.session_id, t.project_slug,
                 t.timestamp, COALESCE(r.result_tokens, t.result_tokens) AS result_tokens,
                 t.baseline_tokens, t.baseline_method,
                 t.resolution, t.followup_within_turn,
                 mr.response_tokens AS replay_tokens, mr.replayed_at, mr.took_ms, mr.ok AS replay_ok
            FROM tool_calls t
            LEFT JOIN tool_calls r
                   ON r.tool_use_id = t.tool_use_id
                  AND r.tool_name = '_tool_result'
                  AND t.tool_use_id IS NOT NULL
            LEFT JOIN mcp_replay mr ON mr.call_id = t.id
           WHERE t.tool_name LIKE 'mcp__%'
           ORDER BY t.timestamp DESC
           LIMIT ?
        """
        rows = [dict(r) for r in c.execute(sql, (limit,))]
        for row in rows:
            if row.get("baseline_tokens") and row.get("result_tokens") is not None:
                row["savings_tokens"] = max(0, row["baseline_tokens"] - row["result_tokens"])
            else:
                row["savings_tokens"] = None
        return rows


def mcp_top_callers(db_path, since=None, until=None, limit: int = 10) -> dict:
    """Top projects + sessions by MCP call count.

    Token totals come from the paired `_tool_result` row's `result_tokens`,
    joined on tool_use_id."""
    rng, args = _range_clause(since, until)
    rng_t = rng.replace("timestamp", "t.timestamp")
    with connect(db_path) as c:
        proj = c.execute(f"""
          SELECT t.project_slug,
                 COUNT(*) AS mcp_calls,
                 COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)),0) AS mcp_tokens,
                 COALESCE(SUM(t.baseline_tokens),0) AS baseline_tokens
            FROM tool_calls t
            LEFT JOIN tool_calls r
                   ON r.tool_use_id = t.tool_use_id
                  AND r.tool_name = '_tool_result'
                  AND t.tool_use_id IS NOT NULL
           WHERE t.tool_name LIKE 'mcp__%' {rng_t}
           GROUP BY t.project_slug
           ORDER BY mcp_calls DESC
           LIMIT ?
        """, (*args, limit)).fetchall()
        sess = c.execute(f"""
          SELECT t.session_id, t.project_slug,
                 COUNT(*) AS mcp_calls,
                 COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)),0) AS mcp_tokens,
                 COALESCE(SUM(t.baseline_tokens),0) AS baseline_tokens,
                 MAX(t.timestamp) AS last_call
            FROM tool_calls t
            LEFT JOIN tool_calls r
                   ON r.tool_use_id = t.tool_use_id
                  AND r.tool_name = '_tool_result'
                  AND t.tool_use_id IS NOT NULL
           WHERE t.tool_name LIKE 'mcp__%' {rng_t}
           GROUP BY t.session_id
           ORDER BY mcp_calls DESC
           LIMIT ?
        """, (*args, limit)).fetchall()
        return {
            "projects": [dict(r) for r in proj],
            "sessions": [dict(r) for r in sess],
        }


def project_mcp_share(db_path, since=None, until=None) -> dict:
    """Map project_slug → {mcp_calls, mcp_tokens, baseline_tokens}."""
    rng, args = _range_clause(since, until)
    rng_t = rng.replace("timestamp", "t.timestamp")
    with connect(db_path) as c:
        rows = c.execute(f"""
          SELECT t.project_slug,
                 COUNT(*) AS mcp_calls,
                 COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)),0) AS mcp_tokens,
                 COALESCE(SUM(t.baseline_tokens),0) AS baseline_tokens
            FROM tool_calls t
            LEFT JOIN tool_calls r
                   ON r.tool_use_id = t.tool_use_id
                  AND r.tool_name = '_tool_result'
                  AND t.tool_use_id IS NOT NULL
           WHERE t.tool_name LIKE 'mcp__%' {rng_t}
           GROUP BY t.project_slug
        """, args).fetchall()
        return {r["project_slug"]: dict(r) for r in rows}


def session_mcp_share(db_path, since=None, until=None) -> dict:
    """Map session_id → {mcp_calls, mcp_tokens, baseline_tokens}."""
    rng, args = _range_clause(since, until)
    rng_t = rng.replace("timestamp", "t.timestamp")
    with connect(db_path) as c:
        rows = c.execute(f"""
          SELECT t.session_id,
                 COUNT(*) AS mcp_calls,
                 COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)),0) AS mcp_tokens,
                 COALESCE(SUM(t.baseline_tokens),0) AS baseline_tokens
            FROM tool_calls t
            LEFT JOIN tool_calls r
                   ON r.tool_use_id = t.tool_use_id
                  AND r.tool_name = '_tool_result'
                  AND t.tool_use_id IS NOT NULL
           WHERE t.tool_name LIKE 'mcp__%' {rng_t}
           GROUP BY t.session_id
        """, args).fetchall()
        return {r["session_id"]: dict(r) for r in rows}


def prompt_mcp_share(db_path, user_uuids: list) -> dict:
    """Map user_uuid → MCP stats for the assistant turn that follows it.

    Pairs by parent_uuid the same way expensive_prompts does. Token totals
    come from the paired _tool_result row joined on tool_use_id."""
    if not user_uuids:
        return {}
    placeholders = ",".join("?" * len(user_uuids))
    with connect(db_path) as c:
        rows = c.execute(f"""
          SELECT u.uuid AS user_uuid,
                 COUNT(t.id) AS mcp_calls,
                 COALESCE(SUM(COALESCE(r.result_tokens, t.result_tokens, 0)),0) AS mcp_tokens,
                 COALESCE(SUM(t.baseline_tokens),0) AS baseline_tokens
            FROM messages u
            JOIN messages a ON a.parent_uuid = u.uuid AND a.type='assistant'
            LEFT JOIN tool_calls t ON t.message_uuid = a.uuid
                                  AND t.tool_name LIKE 'mcp__%'
            LEFT JOIN tool_calls r
                   ON r.tool_use_id = t.tool_use_id
                  AND r.tool_name = '_tool_result'
                  AND t.tool_use_id IS NOT NULL
           WHERE u.uuid IN ({placeholders})
           GROUP BY u.uuid
        """, user_uuids).fetchall()
        return {r["user_uuid"]: dict(r) for r in rows}


def get_mcp_state(db_path, key: str) -> Optional[str]:
    with connect(db_path) as c:
        row = c.execute("SELECT v FROM mcp_state WHERE k=?", (key,)).fetchone()
        return row["v"] if row else None


def set_mcp_state(db_path, key: str, value: str) -> None:
    with connect(db_path) as c:
        c.execute("INSERT OR REPLACE INTO mcp_state (k, v) VALUES (?, ?)", (key, value))
        c.commit()


def recent_sessions(db_path, limit: int = 20, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT session_id, project_slug,
             MIN(timestamp) AS started, MAX(timestamp) AS ended,
             SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
             SUM(input_tokens)+SUM(output_tokens) AS tokens
        FROM messages m
       WHERE 1=1 {rng}
       GROUP BY session_id
       ORDER BY ended DESC
       LIMIT ?
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, (*args, limit))]
        # Cache per-slug name lookups so we don't query once per session.
        slug_cache = {}
        for r in rows:
            slug = r["project_slug"]
            if slug not in slug_cache:
                cwds = [row["cwd"] for row in c.execute(
                    "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                    (slug,),
                )]
                slug_cache[slug] = best_project_name(cwds, slug)
            r["project_name"] = slug_cache[slug]
    return rows


def session_turns(db_path, session_id: str) -> list:
    sql = """
      SELECT uuid, parent_uuid, type, timestamp, model, is_sidechain, agent_id,
             input_tokens, output_tokens, cache_read_tokens,
             cache_create_5m_tokens, cache_create_1h_tokens,
             prompt_text, prompt_chars, tool_calls_json, project_slug, cwd
        FROM messages
       WHERE session_id = ?
       ORDER BY timestamp ASC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (session_id,))]


def daily_token_breakdown(db_path, since=None, until=None) -> list:
    """One row per day: stacked bar data for input/output/cache_read/cache_create."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT substr(timestamp, 1, 10) AS day,
             COALESCE(SUM(input_tokens),0)      AS input_tokens,
             COALESCE(SUM(output_tokens),0)     AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)
               + COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_tokens
        FROM messages
       WHERE timestamp IS NOT NULL {rng}
       GROUP BY day
       ORDER BY day ASC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def skill_breakdown(db_path, since=None, until=None) -> list:
    """Per-skill invocation counts, distinct sessions, last-used timestamp.

    Token attribution per skill is not included: in Claude Code, a Skill's
    content is loaded via a system-reminder on the next turn, not as the
    tool_result body — so `result_tokens` on _tool_result rows reflects the
    activation ack (tiny), not the skill definition (which is what actually
    fills context). A future schema change (storing tool_use_id on the
    invocation row) could enable precise attribution; for now we only expose
    the reliable counts.
    """
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT target AS skill,
             COUNT(*) AS invocations,
             COUNT(DISTINCT session_id) AS sessions,
             MAX(timestamp) AS last_used
        FROM tool_calls
       WHERE tool_name = 'Skill' AND target IS NOT NULL AND target != '' {rng}
       GROUP BY target
       ORDER BY invocations DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def model_breakdown(db_path, since=None, until=None) -> list:
    """Per-model token totals + turn count. Caller computes cost via pricing."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COALESCE(model, 'unknown') AS model,
             COUNT(*) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' {rng}
       GROUP BY model
       ORDER BY (input_tokens + output_tokens + cache_create_5m_tokens + cache_create_1h_tokens) DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]
