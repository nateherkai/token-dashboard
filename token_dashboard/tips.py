"""Rule-based tips engine — produces actionable suggestions from SQLite."""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import List, Optional

from .db import connect
from .pricing import PRICING_JSON, cost_for, load_pricing


def _iso_days_ago(today_iso: str, n: int) -> str:
    d = datetime.fromisoformat(today_iso.replace("Z", ""))
    return (d - timedelta(days=n)).isoformat()


def _key(category: str, scope: str) -> str:
    return f"{category}:{scope}"


def _is_dismissed(db_path, key: str) -> bool:
    with connect(db_path) as c:
        r = c.execute("SELECT dismissed_at FROM dismissed_tips WHERE tip_key=?", (key,)).fetchone()
    if not r:
        return False
    return (time.time() - r["dismissed_at"]) < 14 * 86400


def dismiss_tip(db_path, key: str) -> None:
    with connect(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO dismissed_tips (tip_key, dismissed_at) VALUES (?, ?)",
            (key, time.time()),
        )
        c.commit()


def cache_discipline_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    sql = """
      SELECT project_slug,
             SUM(cache_read_tokens) AS cr,
             SUM(input_tokens + cache_create_5m_tokens + cache_create_1h_tokens) AS rebuild
        FROM messages
       WHERE type='assistant' AND timestamp >= ?
       GROUP BY project_slug
       HAVING (cr + rebuild) > 100000
    """
    out = []
    with connect(db_path) as c:
        for row in c.execute(sql, (since,)):
            total = (row["cr"] or 0) + (row["rebuild"] or 0)
            hit = (row["cr"] or 0) / total if total else 0
            if hit < 0.40:
                key = _key("cache", row["project_slug"])
                if _is_dismissed(db_path, key):
                    continue
                out.append({
                    "key": key,
                    "category": "cache",
                    "title": f"Low cache hit rate in {row['project_slug']}",
                    "body": f"Cache hit rate is {hit*100:.0f}% over the last 7 days. Sessions that restart context frequently rebuild cache. Consider longer-lived sessions or fewer context resets.",
                    "scope": row["project_slug"],
                })
    return out


def repeated_target_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    out = []
    with connect(db_path) as c:
        for row in c.execute("""
          SELECT target, COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions
            FROM tool_calls
           WHERE tool_name IN ('Read','Edit','Write') AND timestamp >= ?
           GROUP BY target HAVING n > 10
           ORDER BY n DESC LIMIT 10
        """, (since,)):
            key = _key("repeat-file", row["target"] or "?")
            if _is_dismissed(db_path, key):
                continue
            out.append({
                "key": key, "category": "repeat-file",
                "title": f"{row['target']} read {row['n']} times",
                "body": f"This file was opened {row['n']} times across {row['sessions']} sessions in the past 7 days. A summary in CLAUDE.md or one read per session would avoid repeats.",
                "scope": row["target"],
            })
        for row in c.execute("""
          SELECT target, COUNT(*) AS n
            FROM tool_calls
           WHERE tool_name='Bash' AND timestamp >= ?
           GROUP BY target HAVING n > 15
           ORDER BY n DESC LIMIT 10
        """, (since,)):
            key = _key("repeat-bash", row["target"] or "?")
            if _is_dismissed(db_path, key):
                continue
            out.append({
                "key": key, "category": "repeat-bash",
                "title": f"`{row['target']}` ran {row['n']} times",
                "body": f"This bash command ran {row['n']} times in the past 7 days. Consider a watch flag or shell alias.",
                "scope": row["target"],
            })
    return out


def right_size_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    sql = """
      SELECT model, COUNT(*) AS n,
             SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
             SUM(cache_read_tokens) AS cache_read_tokens,
             SUM(cache_create_5m_tokens) AS cache_create_5m_tokens,
             SUM(cache_create_1h_tokens) AS cache_create_1h_tokens
        FROM messages
       WHERE type='assistant' AND model LIKE '%opus%'
         AND output_tokens < 500 AND is_sidechain = 0
         AND timestamp >= ?
       GROUP BY model
    """
    with connect(db_path) as c:
        rows = c.execute(sql, (since,)).fetchall()
    n = sum(r["n"] for r in rows)
    if n < 10:
        return []
    pricing = load_pricing(PRICING_JSON)
    api_opus = api_sonnet = 0.0
    for r in rows:
        usage = {k: r[k] or 0 for k in r.keys() if k.endswith("_tokens")}
        api_opus += cost_for(r["model"], usage, pricing)["usd"] or 0
        # Same-generation Sonnet when the file lists it, else the sonnet tier fallback.
        api_sonnet += cost_for(r["model"].replace("opus", "sonnet"), usage, pricing)["usd"] or 0
    savings = api_opus - api_sonnet
    if savings < 1.0:
        return []
    key = _key("right-size", "opus-short-turns-7d")
    if _is_dismissed(db_path, key):
        return []
    return [{
        "key": key, "category": "right-size",
        "title": f"{n} short Opus turns might fit on Sonnet",
        "body": f"Opus turns under 500 output tokens cost ~${api_opus:.2f} in the last 7 days. Sonnet would have cost ~${api_sonnet:.2f} (savings ~${savings:.2f}).",
        "scope": "opus-short-turns-7d",
    }]


def outlier_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    out = []
    with connect(db_path) as c:
        big = c.execute("""
          SELECT COUNT(*) AS n, AVG(result_tokens) AS avg_t
            FROM tool_calls
           WHERE tool_name='_tool_result' AND result_tokens > 50000 AND timestamp >= ?
        """, (since,)).fetchone()
        if big and (big["n"] or 0) >= 5:
            key = _key("tool-bloat", "result-50k+")
            if not _is_dismissed(db_path, key):
                out.append({
                    "key": key, "category": "tool-bloat",
                    "title": f"{big['n']} tool results over 50k tokens this week",
                    "body": f"Average size is {int(big['avg_t']):,} tokens. Pipe long Bash output to head/tail and ask for narrower file reads.",
                    "scope": "result-50k+",
                })
        for row in c.execute("""
          SELECT agent_id, COUNT(*) AS n,
                 AVG(input_tokens+output_tokens) AS mean_t,
                 MAX(input_tokens+output_tokens) AS max_t
            FROM messages
           WHERE is_sidechain=1 AND agent_id IS NOT NULL AND timestamp >= ?
           GROUP BY agent_id HAVING n >= 10
        """, (since,)):
            if (row["max_t"] or 0) > 6 * (row["mean_t"] or 1) and (row["max_t"] or 0) > 50_000:
                key = _key("subagent-outlier", row["agent_id"])
                if _is_dismissed(db_path, key):
                    continue
                out.append({
                    "key": key, "category": "subagent-outlier",
                    "title": f"Subagent {row['agent_id']} has cost outliers",
                    "body": f"Largest invocation used {int(row['max_t']):,} tokens vs mean {int(row['mean_t']):,}. Worth checking what those did differently.",
                    "scope": row["agent_id"],
                })
    return out


def all_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    return [
        *cache_discipline_tips(db_path, today_iso),
        *repeated_target_tips(db_path, today_iso),
        *right_size_tips(db_path, today_iso),
        *outlier_tips(db_path, today_iso),
    ]
