#!/usr/bin/env python3
"""Compare SQLite vs HeliosDB read paths. Run with TD_PRIMARY_STORE=helios set.

Compares the headline metrics produced by db.py (SQLite) and db_helios.py
(HeliosDB) for the same query. Pass when the relative difference is within
tolerance for each metric. Used as the gate for flipping the read dispatcher
to helios. Reads the SQLite cache path from TOKEN_DASHBOARD_DB and the target
DSN from HELIOSDB_DSN.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from token_dashboard import db, db_helios

DB_PATH = os.environ.get("TOKEN_DASHBOARD_DB",
                         os.path.expanduser("~/.claude/token-dashboard.db"))


def _approx(a, b, rel=0.01):
    """Within `rel` relative diff (default 1%)."""
    a = a or 0
    b = b or 0
    if a == 0 and b == 0:
        return True
    base = max(abs(a), abs(b))
    return abs(a - b) / base <= rel


def _section(title):
    print(f"\n=== {title} ===")


def cmp_overview():
    _section("/api/overview")
    s = db.overview_totals(DB_PATH)
    h = db_helios.overview_totals(DB_PATH)
    keys = ("sessions", "turns", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_create_5m_tokens", "cache_create_1h_tokens")
    rows = []
    ok = True
    for k in keys:
        sa, ha = s.get(k, 0), h.get(k, 0)
        match = _approx(sa, ha, rel=0.01)
        ok = ok and match
        rows.append((k, sa, ha, "OK" if match else "DIFF"))
    for r in rows:
        print(f"  {r[0]:30}  sqlite={r[1]:>12}  helios={r[2]:>12}  {r[3]}")
    return ok


def cmp_projects():
    _section("/api/projects")
    s = db.project_summary(DB_PATH)
    h = db_helios.project_summary(DB_PATH)
    print(f"  sqlite projects={len(s)}  helios projects={len(h)}")
    s_top = sorted(s, key=lambda x: -(x.get("billable_tokens") or 0))[:8]
    h_top = sorted(h, key=lambda x: -(x.get("billable_tokens") or 0))[:8]
    print("  top-8 by billable (slug, sqlite tokens, helios tokens):")
    s_map = {r["project_slug"]: r for r in s}
    h_map = {r["project_slug"]: r for r in h}
    ok = True
    for r in s_top:
        slug = r["project_slug"]
        sb = r.get("billable_tokens") or 0
        hb = (h_map.get(slug) or {}).get("billable_tokens") or 0
        match = _approx(sb, hb, rel=0.01)
        ok = ok and match
        print(f"    {slug[:35]:35} {sb:>12} {hb:>12} {'OK' if match else 'DIFF'}")
    return ok


def cmp_sessions():
    _section("/api/sessions (most recent 10)")
    s = db.recent_sessions(DB_PATH, limit=10)
    h = db_helios.recent_sessions(DB_PATH, limit=10)
    print(f"  sqlite={len(s)} helios={len(h)}")
    if not s:
        return True
    sids_s = [r["session_id"] for r in s]
    sids_h = [r["session_id"] for r in h]
    overlap = len(set(sids_s) & set(sids_h))
    print(f"  session_id overlap: {overlap}/10")
    return overlap >= 8


def cmp_tools():
    _section("/api/tools breakdown")
    s = db.tool_token_breakdown(DB_PATH)
    h = db_helios.tool_token_breakdown(DB_PATH)
    s_map = {r["tool_name"]: r for r in s}
    h_map = {r["tool_name"]: r for r in h}
    common = set(s_map) & set(h_map)
    print(f"  tools sqlite={len(s_map)}  helios={len(h_map)}  common={len(common)}")
    ok = True
    for name in sorted(common, key=lambda n: -s_map[n]["calls"])[:6]:
        sa, ha = s_map[name]["calls"], h_map[name]["calls"]
        match = _approx(sa, ha, rel=0.01)
        ok = ok and match
        print(f"    {name:30}  sqlite={sa:>10}  helios={ha:>10}  {'OK' if match else 'DIFF'}")
    return ok


def cmp_daily():
    _section("/api/daily")
    s = db.daily_token_breakdown(DB_PATH)
    h = db_helios.daily_token_breakdown(DB_PATH)
    print(f"  sqlite days={len(s)}  helios days={len(h)}")
    return abs(len(s) - len(h)) <= 1


def cmp_models():
    _section("/api/by-model")
    s = db.model_breakdown(DB_PATH)
    h = db_helios.model_breakdown(DB_PATH)
    s_map = {(r.get("model") or "unknown"): r for r in s}
    h_map = {(r.get("model") or "unknown"): r for r in h}
    print(f"  sqlite models={list(s_map)[:5]}")
    print(f"  helios models={list(h_map)[:5]}")
    common = set(s_map) & set(h_map)
    return len(common) >= max(1, len(s_map) - 1)


def main():
    print("Cutover validation — comparing SQLite vs HeliosDB on the same queries")
    print(f"  DB_PATH: {DB_PATH}")
    print(f"  HELIOSDB_DSN: {(os.environ.get('HELIOSDB_DSN') or '<unset>')[:60]}…")

    started = time.time()
    results = {}
    for name, fn in [
        ("overview",  cmp_overview),
        ("projects",  cmp_projects),
        ("sessions",  cmp_sessions),
        ("tools",     cmp_tools),
        ("daily",     cmp_daily),
        ("by-model",  cmp_models),
    ]:
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = False
            print(f"  ERROR in {name}: {type(e).__name__}: {e}")
    elapsed = time.time() - started
    print(f"\n=== Summary (validation took {elapsed:.1f}s) ===")
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        print(f"  {name:12}  {'PASS' if ok else 'FAIL'}")
    print(f"\n  {passed}/{total} checks pass")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
