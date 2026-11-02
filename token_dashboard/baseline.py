"""MCP-call baseline-token estimator + backfill.

For each MCP tool call, estimate how many tokens an equivalent native-tool
recipe (Grep + Read + Bash) would have consumed. The estimate is rough and
project-relative — it uses the project's average `Read` response size as
the per-file baseline. Always labelled 'estimated' so the UI can surface
that this is not a measurement.
"""
from __future__ import annotations

import json
from typing import Optional

from .db import connect


MCP_PREFIX = "mcp__"
DEFAULT_FILE_TOKENS = 4000.0  # fallback when project has no observed Reads


def _project_avg_read_tokens(conn, project_slug: str) -> float:
    row = conn.execute(
        "SELECT AVG(result_tokens) AS a FROM tool_calls "
        "WHERE tool_name='_tool_result' AND project_slug=? "
        "AND result_tokens IS NOT NULL AND result_tokens > 0",
        (project_slug,),
    ).fetchone()
    v = row["a"] if row else None
    return float(v) if v else 0.0


def _global_avg_read_tokens(conn) -> float:
    row = conn.execute(
        "SELECT AVG(result_tokens) AS a FROM tool_calls "
        "WHERE tool_name='_tool_result' AND result_tokens IS NOT NULL AND result_tokens > 0"
    ).fetchone()
    v = row["a"] if row else None
    return float(v) if v else DEFAULT_FILE_TOKENS


def estimate_baseline(tool_name: str, target: Optional[str], project_avg: float) -> Optional[int]:
    """Rule library — counterfactual cost in tokens for an MCP call.

    Returns None when no native-tool recipe applies (e.g. indexing/installation
    tools have no equivalent — they get baseline_method='tracking_only').
    """
    if not tool_name.startswith(MCP_PREFIX):
        return None
    short = tool_name[len(MCP_PREFIX):].lower()
    avg = project_avg or DEFAULT_FILE_TOKENS

    # Semantic / graph search → 3 file Reads + Grep header.
    if any(k in short for k in ("graphrag_search", "graph_search", "search_code", "code_search", "semantic_search", "search_symbols")):
        return int(3 * avg + 100)

    # LSP definition / hover → 1 file Read window (~50 lines ≈ 0.4 of full file) + Grep.
    if any(k in short for k in ("lsp_definition", "lsp_hover", "go_to_definition", "symbol_def", "definition_at")):
        return int(0.4 * avg + 200)

    # LSP references → project-wide Grep + 2 file Reads.
    if any(k in short for k in ("lsp_references", "find_references", "references_to", "callers_of")):
        return int(2 * avg + 500)

    # Rename / refactor preview → multi-file scan + edit (n files).
    if any(k in short for k in ("rename", "refactor", "rewrite_apply")):
        return int(1.5 * avg + 300)

    # WITH CONTEXT / get_context / context_for_symbol → 2 reads worth of context.
    if "context" in short:
        return int(2 * avg + 200)

    # Doc query → 1 Grep + 1 Read.
    if any(k in short for k in ("doc_search", "ask_docs", "graphrag_ask")):
        return int(1.2 * avg + 200)

    # Indexing / install / hooks have no native equivalent; track only.
    if any(k in short for k in ("ingest", "index", "hook", "install", "init", "register",
                                 "ping", "health", "status", "info", "version")):
        return None

    # Generic MCP fallback — assume one Read-equivalent.
    return int(avg)


def _parse_meta(meta_json: Optional[str]) -> dict:
    if not meta_json:
        return {}
    try:
        v = json.loads(meta_json)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def backfill(db_path: str, only_mcp: bool = True) -> dict:
    """Populate baseline_tokens / baseline_method / followup_within_turn / resolution.

    Idempotent: only touches rows where the corresponding column is NULL.
    Safe to call repeatedly (e.g. once per scan loop).
    """
    n_baseline = 0
    n_tracking_only = 0
    n_followup = 0
    n_resolution = 0
    with connect(db_path) as c:
        # ---- baseline_tokens ----
        proj_cache: dict[str, float] = {}
        global_avg = _global_avg_read_tokens(c) or DEFAULT_FILE_TOKENS
        cur = c.execute(
            "SELECT id, project_slug, tool_name, target, mcp_meta_json "
            "FROM tool_calls "
            "WHERE tool_name LIKE 'mcp__%' AND baseline_method IS NULL"
        )
        rows = cur.fetchall()
        for r in rows:
            slug = r["project_slug"]
            if slug not in proj_cache:
                v = _project_avg_read_tokens(c, slug)
                proj_cache[slug] = v if v else global_avg
            est = estimate_baseline(r["tool_name"], r["target"], proj_cache[slug])
            if est is None:
                c.execute(
                    "UPDATE tool_calls SET baseline_method='tracking_only' WHERE id=?",
                    (r["id"],),
                )
                n_tracking_only += 1
            else:
                c.execute(
                    "UPDATE tool_calls SET baseline_tokens=?, baseline_method='estimated' WHERE id=?",
                    (est, r["id"]),
                )
                n_baseline += 1

        # ---- followup_within_turn (for ALL tool_calls, not just MCP) ----
        # Count subsequent non-`_tool_result` siblings in the same assistant turn.
        cur = c.execute(
            "SELECT id, message_uuid FROM tool_calls "
            "WHERE followup_within_turn IS NULL AND tool_name != '_tool_result'"
        )
        pending = cur.fetchall()
        for r in pending:
            count_row = c.execute(
                "SELECT COUNT(*) AS n FROM tool_calls "
                "WHERE message_uuid=? AND id > ? AND tool_name != '_tool_result'",
                (r["message_uuid"], r["id"]),
            ).fetchone()
            c.execute(
                "UPDATE tool_calls SET followup_within_turn=? WHERE id=?",
                (count_row["n"], r["id"]),
            )
            n_followup += 1

        # ---- resolution from mcp_meta_json (#199 surface) ----
        cur = c.execute(
            "SELECT id, mcp_meta_json FROM tool_calls "
            "WHERE tool_name LIKE 'mcp__%' AND resolution IS NULL "
            "AND mcp_meta_json IS NOT NULL"
        )
        for r in cur.fetchall():
            meta = _parse_meta(r["mcp_meta_json"])
            res = meta.get("resolution") or (meta.get("helios") or {}).get("resolution")
            if isinstance(res, str) and res in ("exact", "heuristic", "unresolved"):
                c.execute("UPDATE tool_calls SET resolution=? WHERE id=?", (res, r["id"]))
                n_resolution += 1
        c.commit()

    return {
        "baseline_set":    n_baseline,
        "tracking_only":   n_tracking_only,
        "followup_set":    n_followup,
        "resolution_set":  n_resolution,
    }
