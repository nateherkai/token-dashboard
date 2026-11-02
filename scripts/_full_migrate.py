#!/usr/bin/env python3
"""One-shot full bulk migration from SQLite to HeliosDB.

Requires HELIOSDB_DSN in the environment (a postgresql:// DSN for the target
HeliosDB instance). Reads the SQLite cache path from TOKEN_DASHBOARD_DB.

    HELIOSDB_DSN=postgresql://user:pass@host:5432/db \
    TOKEN_DASHBOARD_DB=/path/to/token-dashboard.db \
    python3 scripts/_full_migrate.py
"""
import os, sys, time, json

if not os.environ.get("HELIOSDB_DSN"):
    sys.exit("HELIOSDB_DSN not set — export it before running this migration")

# Make the package importable when run from a checkout.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from token_dashboard import bulk_migrate

DB_PATH = os.environ.get("TOKEN_DASHBOARD_DB",
                         os.path.expanduser("~/.claude/token-dashboard.db"))
LIMIT = int(os.environ.get("TD_MIGRATE_LIMIT", "600000"))
RESULT = os.environ.get("TD_MIGRATE_RESULT", "/tmp/migrate-result.json")

t0 = time.time()
print(f"starting migrate from {DB_PATH}, limit={LIMIT}", flush=True)
r = bulk_migrate.migrate(DB_PATH, limit_messages=LIMIT)
r["total_elapsed_s"] = round(time.time() - t0, 1)
with open(RESULT, "w") as f:
    json.dump(r, f, indent=2)
print(json.dumps(r, indent=2), flush=True)
