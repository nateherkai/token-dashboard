"""HeliosDB Nano live introspection over Postgres wire (5432).

Uses the HeliosDB 3.19.x schema-namespaced surface (`_hdb_code.*`,
`_hdb_graph.*`) to expose corpus size, embedded-body coverage, supported
languages, and last-index time. All queries are read-only and best-effort —
each one is wrapped so a failure on one query (e.g. a missing extension)
doesn't blank out the whole panel.

Connection via pg8000 (pure-Python, no libpq). Configured by:
  HELIOSDB_DSN  — postgresql://user:pass@host:5432/dbname
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional
from urllib.parse import urlparse


def dsn() -> Optional[str]:
    return os.environ.get("HELIOSDB_DSN")


def is_configured() -> bool:
    return bool(dsn())


def _connect():
    s = dsn()
    if not s:
        return None
    p = urlparse(s)
    if p.scheme not in ("postgres", "postgresql"):
        raise RuntimeError(f"unsupported DSN scheme: {p.scheme}")
    import pg8000.dbapi  # lazy import — package is optional
    conn = pg8000.dbapi.connect(
        host=p.hostname or "localhost",
        port=p.port or 5432,
        user=p.username or "postgres",
        password=p.password or "",
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        timeout=8,
    )
    # Same v3.26 quirk as the writer: HeliosDB rejects pg8000's implicit
    # `BEGIN` with "Transaction already active". Use autocommit.
    conn.autocommit = True
    return conn


def _scalar(cur, sql: str) -> Any:
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        return {"error": _short(e)}


def _list(cur, sql: str, col: int = 0) -> Any:
    try:
        cur.execute(sql)
        return [r[col] for r in cur.fetchall()]
    except Exception as e:
        return {"error": _short(e)}


def _first_ok(cur, sqls: list) -> Any:
    """Try alternate query shapes; return the first that succeeds."""
    last_err = None
    for sql in sqls:
        try:
            cur.execute(sql)
            r = cur.fetchall()
            return r if isinstance(r, list) else r
        except Exception as e:
            last_err = e
    return {"error": _short(last_err)}


def _short(e: Exception) -> str:
    s = f"{type(e).__name__}: {e}"
    return s[:240]


def pulse() -> dict:
    """Live HeliosDB metrics. Always returns a dict (never raises)."""
    if not is_configured():
        return {"configured": False, "reason": "HELIOSDB_DSN not set"}
    started = time.time()
    out: dict = {"configured": True, "ok": False, "ts": started, "endpoint": _redact_dsn()}
    conn = None
    try:
        conn = _connect()
    except ModuleNotFoundError:
        out["error"] = "pg8000 not installed in the dashboard image"
        return out
    except Exception as e:
        out["error"] = f"connect failed: {_short(e)}"
        return out
    if conn is None:
        out["error"] = "no connection"
        return out

    try:
        cur = conn.cursor()

        # Server-side identity (works on Postgres + on Helios's PG-compat layer)
        out["server_version"] = _scalar(cur, "SELECT version()")

        # Schema visibility (#198 — composite catalog keys)
        out["schemas"] = _list(cur,
            "SELECT nspname FROM pg_namespace ORDER BY nspname")

        # Corpus — graph-rag side
        out["nodes_total"] = _first_scalar(cur, [
            "SELECT COUNT(*) FROM _hdb_graph.nodes",
            "SELECT COUNT(*) FROM _hdb_graph_nodes",
        ])
        out["nodes_docchunk"] = _first_scalar(cur, [
            "SELECT COUNT(*) FROM _hdb_graph.nodes WHERE node_kind='DocChunk'",
            "SELECT COUNT(*) FROM _hdb_graph_nodes WHERE node_kind='DocChunk'",
        ])
        out["nodes_docsection"] = _first_scalar(cur, [
            "SELECT COUNT(*) FROM _hdb_graph.nodes WHERE node_kind='DocSection'",
            "SELECT COUNT(*) FROM _hdb_graph_nodes WHERE node_kind='DocSection'",
        ])
        out["edges_total"] = _first_scalar(cur, [
            "SELECT COUNT(*) FROM _hdb_graph.edges",
            "SELECT COUNT(*) FROM _hdb_graph_edges",
        ])

        # Code-graph side
        out["files_total"] = _first_scalar(cur, [
            "SELECT COUNT(*) FROM _hdb_code.files",
            "SELECT COUNT(*) FROM _hdb_code_files",
        ])
        out["symbols_total"] = _first_scalar(cur, [
            "SELECT COUNT(*) FROM _hdb_code.symbols",
            "SELECT COUNT(*) FROM _hdb_code_symbols",
        ])
        out["symbols_with_body_vec"] = _first_scalar(cur, [
            "SELECT COUNT(*) FROM _hdb_code.symbols WHERE body_vec IS NOT NULL",
            "SELECT COUNT(*) FROM _hdb_code_symbols WHERE body_vec IS NOT NULL",
        ])
        out["last_indexed"] = _first_scalar(cur, [
            "SELECT MAX(scanned_at)::text FROM _hdb_code.files",
            "SELECT MAX(scanned_at)::text FROM _hdb_code_files",
        ])

        # Resolution quality (#189/#199 — `resolution` column on symbol_refs)
        out["refs_exact"] = _first_scalar(cur, [
            "SELECT COUNT(*) FROM _hdb_code.symbol_refs WHERE resolution='exact'",
            "SELECT COUNT(*) FROM _hdb_code_symbol_refs WHERE resolution='exact'",
        ])
        out["refs_heuristic"] = _first_scalar(cur, [
            "SELECT COUNT(*) FROM _hdb_code.symbol_refs WHERE resolution='heuristic'",
            "SELECT COUNT(*) FROM _hdb_code_symbol_refs WHERE resolution='heuristic'",
        ])
        out["refs_unresolved"] = _first_scalar(cur, [
            "SELECT COUNT(*) FROM _hdb_code.symbol_refs WHERE resolution='unresolved'",
            "SELECT COUNT(*) FROM _hdb_code_symbol_refs WHERE resolution='unresolved'",
        ])

        # Languages (#181). Defensive flatten — pg8000+HeliosDB v3.26 has
        # been observed returning each row as a single-item list AND wrapping
        # the string value in another list, giving [['go'], ['javascript']]
        # instead of ['go','javascript']. Normalise to flat strings here so
        # the UI's htmlSafe() doesn't trip on array values.
        langs = _first_ok(cur, [
            "SELECT name FROM hdb_code_languages ORDER BY name",
            "SELECT name FROM _hdb_code.languages ORDER BY name",
        ])
        if isinstance(langs, (list, tuple)):
            flat = []
            for r in langs:
                v = r[0] if (isinstance(r, (list, tuple)) and len(r) > 0) else r
                # If the inner value is itself a list/tuple, keep unwrapping.
                while isinstance(v, (list, tuple)) and len(v) == 1:
                    v = v[0]
                if v is not None:
                    flat.append(str(v))
            out["languages"] = flat
        else:
            out["languages"] = langs

        # Coverage % for body_vec
        if isinstance(out.get("symbols_total"), int) and isinstance(out.get("symbols_with_body_vec"), int) and out["symbols_total"]:
            out["body_vec_coverage"] = round(out["symbols_with_body_vec"] / out["symbols_total"], 4)
        # Resolution split %
        rx = out.get("refs_exact")
        rh = out.get("refs_heuristic")
        ru = out.get("refs_unresolved")
        if all(isinstance(v, int) for v in (rx, rh, ru)):
            tot = (rx or 0) + (rh or 0) + (ru or 0)
            if tot:
                out["exact_resolution_rate"] = round(rx / tot, 4)
        out["ok"] = True
    except Exception as e:
        out["error"] = _short(e)
    finally:
        try: conn.close()
        except Exception: pass
    out["query_ms"] = int((time.time() - started) * 1000)
    return out


def _first_scalar(cur, sqls: list):
    """Run the first sql that succeeds, return its scalar value."""
    last = None
    for sql in sqls:
        try:
            cur.execute(sql)
            r = cur.fetchone()
            return r[0] if r else None
        except Exception as e:
            last = e
            try: cur.execute("ROLLBACK")
            except Exception: pass
    return {"error": _short(last) if last else "unknown"}


def _redact_dsn() -> str:
    s = dsn() or ""
    p = urlparse(s)
    user = p.username or ""
    host = p.hostname or ""
    port = f":{p.port}" if p.port else ""
    db = (p.path or "/").lstrip("/")
    return f"{p.scheme}://{user}@{host}{port}/{db}"
