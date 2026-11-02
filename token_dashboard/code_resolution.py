"""Item #5 — Code-symbol resolution for prompts.

For each project whose `cwd` is reachable from the dashboard host, we
trigger HeliosDB's code-graph indexer over the project root, then look
up symbols mentioned in prompts via `helios_lsp_definition`.

If the running HeliosDB build doesn't expose the index helpers (e.g.
because we built without `code-graph`), the dashboard surfaces a clear
"unavailable" status rather than failing.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional

from . import helios_writer as hw


# Identifier-shaped tokens — letters/digits/underscore, at least 4 chars.
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\b")


def index_project(cwd: str) -> dict:
    """Run HeliosDB's code-graph index over a path. Returns
    {ok, files_indexed, symbols_indexed} or an error dict.

    HeliosDB v3.26 exposes code-graph indexing via the binary CLI
    (`heliosdb-nano code-graph index <path>`), not the SQL surface — so
    via the dashboard we surface the *current* index state by reading
    the system tables. The dashboard host can shell out to the binary if
    indexing-on-demand is wanted.
    """
    conn = hw.get_conn()
    if conn is None:
        return {"ok": False, "error": "helios not configured"}
    # Each attempt uses a fresh cursor so a failure on one doesn't poison
    # the next.
    for query, parser in [
        ("SELECT helios_code_index($1)",                 lambda r: ("raw", r[0] if r else None)),
        ("SELECT COUNT(*) FROM _hdb_code_files",         lambda r: ("files", int(r[0]) if r else 0)),
    ]:
        try:
            cur = conn.cursor()
            if "$1" in query:
                cur.execute(query, (cwd,))
            else:
                cur.execute(query)
            row = cur.fetchone()
            key, val = parser(row)
            if key == "raw":
                return {"ok": True, "raw": val,
                        "note": "called helios_code_index SQL helper"}
            # Fell into table-count path — fetch symbols too
            files_n = val
            cur2 = conn.cursor()
            cur2.execute("SELECT COUNT(*) FROM _hdb_code_symbols")
            sym_n = int(cur2.fetchone()[0])
            return {"ok": True, "files": files_n, "symbols": sym_n, "cwd": cwd,
                    "note": "code-graph indexer is exposed via the heliosdb-nano CLI binary, not via SQL — "
                            "run `heliosdb-nano code-graph index <path>` against the same data dir"}
        except Exception:
            continue
    return {"ok": False, "error": "_hdb_code tables not present (build missing code-graph feature?) — index via the heliosdb-nano binary"}


def resolve_in_prompt(prompt_text: str, max_lookups: int = 6) -> list:
    """Pluck identifier-shaped tokens out of a prompt and look each up
    via helios_lsp_definition. Returns a list of resolved/heuristic hits."""
    if not prompt_text:
        return []
    candidates = list(dict.fromkeys(_IDENT_RE.findall(prompt_text)))[:max_lookups]
    if not candidates:
        return []
    conn = hw.get_conn()
    if conn is None:
        return []
    cur = conn.cursor()
    out = []
    for ident in candidates:
        for sql in (
            "SELECT name, kind, file_path, line, resolution FROM helios_lsp_definition($1) LIMIT 3",
            "SELECT name, kind, path, line FROM _hdb_code_symbols WHERE name = $1 LIMIT 3",
        ):
            try:
                cur.execute(sql, (ident,))
                rows = cur.fetchall()
                if rows:
                    for r in rows:
                        rec = {"identifier": ident, "name": r[0],
                               "kind": r[1] if len(r) > 1 else None,
                               "file": r[2] if len(r) > 2 else None,
                               "line": r[3] if len(r) > 3 else None}
                        if len(r) > 4:
                            rec["resolution"] = r[4]
                        out.append(rec)
                    break
            except Exception:
                try: conn.rollback()
                except Exception: pass
                continue
    return out


def code_graph_status() -> dict:
    """Health check — used by the MCP tab to show whether code-graph is on."""
    conn = hw.get_conn()
    if conn is None:
        return {"available": False, "reason": "helios not configured"}
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM _hdb_code_symbols")
        n = int(cur.fetchone()[0])
        return {"available": True, "indexed_symbols": n}
    except Exception as e:
        return {"available": False, "reason": str(e)[:200]}
