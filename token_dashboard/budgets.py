"""Project spend guardrails backed by the dashboard's shared database."""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from .db import connect
from .pricing import cost_for, load_pricing


def set_budget(db_path, project_slug: str, daily_usd=None, monthly_usd=None, webhook=None):
    if not project_slug.strip():
        raise ValueError("project_slug is required")
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO project_budgets(project_slug,daily_usd,monthly_usd,alert_webhook,updated_at) VALUES(?,?,?,?,?)",
            (project_slug, daily_usd, monthly_usd, webhook, time.time()),
        )
        conn.commit()
    return get_budgets(db_path, project_slug)


def get_budgets(db_path, project_slug=None):
    with connect(db_path) as conn:
        if project_slug:
            row = conn.execute("SELECT * FROM project_budgets WHERE project_slug=?", (project_slug,)).fetchone()
            return dict(row) if row else None
        return [dict(row) for row in conn.execute("SELECT * FROM project_budgets ORDER BY project_slug")]


def _project_cost(db_path, project_slug, pricing, since=None, until=None):
    predicates = ["type = 'assistant'", "project_slug = ?"]
    args = [project_slug]
    if since:
        predicates.append("timestamp >= ?")
        args.append(since)
    if until:
        predicates.append("timestamp <= ?")
        args.append(until)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT model, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens, "
            "SUM(cache_read_tokens) cache_read_tokens, SUM(cache_create_5m_tokens) cache_create_5m_tokens, "
            "SUM(cache_create_1h_tokens) cache_create_1h_tokens FROM messages WHERE "
            + " AND ".join(predicates) + " GROUP BY model", args).fetchall()
    return sum(_cost(dict(row), pricing) for row in rows)


def _cost(row, pricing):
    usage = {key: row.get(key, 0) or 0 for key in (
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_create_5m_tokens", "cache_create_1h_tokens")}
    return cost_for(row.get("model", ""), usage, pricing).get("usd") or 0.0


def check_budgets(db_path, pricing_path: str | Path, since=None, until=None):
    pricing = load_pricing(pricing_path)
    budgets = {row["project_slug"]: row for row in get_budgets(db_path)}
    results = []
    for slug, budget in budgets.items():
        cost = _project_cost(db_path, slug, pricing, since, until)
        daily_limit = budget.get("daily_usd")
        monthly_limit = budget.get("monthly_usd")
        for period, limit in (("daily", daily_limit), ("monthly", monthly_limit)):
            if limit is not None and cost >= float(limit):
                result = {"project_slug": slug, "period": period, "spent_usd": cost, "limit_usd": float(limit), "exceeded": True}
                results.append(result)
                webhook = budget.get("alert_webhook")
                if webhook:
                    try:
                        request = urllib.request.Request(webhook, data=json.dumps(result).encode(), headers={"Content-Type": "application/json"})
                        urllib.request.urlopen(request, timeout=5).read()
                    except Exception:
                        pass
    return results
