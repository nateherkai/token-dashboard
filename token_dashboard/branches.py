"""Item #7 — Branch-based time travel.

Wraps HeliosDB's branch-creation surface so the dashboard can take a
snapshot of `dashboard.*` tables on a schedule. Read queries can then
dispatch with `ON BRANCH '<name>'` to view the dashboard "as of" any
prior date.

Branches are exposed via the SQL surface as
  `CREATE BRANCH '<name>' FROM 'main'` (per HeliosDB FR-3 / #5).
Falls back to a polite error if the running binary doesn't expose the
branch DDL — the rest of the dashboard keeps working.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional

from . import helios_writer as hw

# HeliosDB's CREATE BRANCH / ON BRANCH clauses take the branch name as a SQL
# string literal and do NOT support bound parameters for it, so the name is
# interpolated. It is user-reachable (POST /api/branches/snapshot {name},
# GET /api/branches/overview?branch=), so it must be whitelisted to a strict
# identifier charset before it ever touches SQL — anything else is injection.
_BRANCH_NAME_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}\Z")


def _is_valid_branch_name(name: str) -> bool:
    return isinstance(name, str) and bool(_BRANCH_NAME_RE.match(name))


def daily_branch_name(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return f"td-snap-{dt.strftime('%Y%m%d')}"


def create_snapshot(name: Optional[str] = None) -> dict:
    name = name or daily_branch_name()
    if not _is_valid_branch_name(name):
        return {"ok": False, "error": "invalid branch name "
                "(allowed: letters, digits, '_', '-'; max 64 chars)"}
    conn = hw.get_conn()
    if conn is None:
        return {"ok": False, "error": "helios not configured"}
    cur = conn.cursor()
    started = time.time()
    # HeliosDB v3.26 requires `AS OF` clause; the parent branch name varies.
    last = None
    for sql in [
        f"CREATE BRANCH '{name}' FROM 'main' AS OF NOW",
        f"CREATE BRANCH '{name}' AS OF NOW",
        f"CREATE BRANCH '{name}' FROM 'default' AS OF NOW",
        f"CREATE BRANCH '{name}'",
    ]:
        try:
            cur.execute(sql)
            hw._commit(conn)
            return {"ok": True, "branch": name,
                    "elapsed_ms": int((time.time()-started)*1000),
                    "syntax": sql}
        except Exception as e:
            last = e
    return {"ok": False, "branch": name,
            "error": str(last)[:240] if last else "no syntax worked"}


def list_snapshots() -> list:
    conn = hw.get_conn()
    if conn is None:
        return []
    cur = conn.cursor()
    # The branches view name varies; try common shapes.
    for sql in [
        "SELECT branch_name, created_at FROM _hdb_branches WHERE branch_name LIKE 'td-snap-%' ORDER BY created_at DESC",
        "SELECT name, created_at FROM pg_catalog.helios_branches WHERE name LIKE 'td-snap-%' ORDER BY created_at DESC",
        "SELECT name, ts FROM helios_branches WHERE name LIKE 'td-snap-%' ORDER BY ts DESC",
    ]:
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            return [{"branch": r[0], "created_at": str(r[1])} for r in rows]
        except Exception:
            try: conn.rollback()
            except Exception: pass
    return []


def overview_as_of(branch: str) -> dict:
    """Re-run the headline overview on a snapshot branch."""
    if not _is_valid_branch_name(branch):
        return {"error": "invalid branch name"}
    conn = hw.get_conn()
    if conn is None:
        return {"error": "helios not configured"}
    cur = conn.cursor()
    try:
        # `ON BRANCH '<name>'` per FR-3 docs
        cur.execute(f"""
          SELECT COUNT(DISTINCT session_id) AS sessions,
                 SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
                 COALESCE(SUM(input_tokens),0),
                 COALESCE(SUM(output_tokens),0),
                 COALESCE(SUM(cache_read_tokens),0)
            FROM dashboard.messages
            ON BRANCH '{branch}'
        """)
        r = cur.fetchone()
        if not r:
            return {"error": "no data on branch"}
        return {"sessions": r[0], "turns": r[1],
                "input_tokens": r[2], "output_tokens": r[3],
                "cache_read_tokens": r[4], "branch": branch}
    except Exception as e:
        # Try main if branch syntax not supported
        return {"error": str(e)[:240], "branch": branch}
