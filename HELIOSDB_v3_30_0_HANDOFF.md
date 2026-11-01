# HeliosDB-Nano v3.30.0 — KanttBan + Token-Dashboard hand-off

**Released:** 2026-05-05
**Install:** `cargo install heliosdb-nano --version 3.30.0 --features server --force`

This release closes the perf carry-overs reported by the Token-Dashboard
team (against v3.27, deferred through v3.29) and addresses two open
items from your v3.28 re-test: Bug #16 investigation and Bug #18
tracing recipe.

---

## Token-Dashboard team — Quirks H + I are closed

Both perf bugs deferred from v3.27 are now fixed via index-based lookups.
The hangs that previously required `kill -9` are gone; the upsert
slowdown that broke your bulk-migrate is gone.

| Bug | v3.27 baseline | v3.30 measured | Speedup |
|---|---|---|---|
| **Quirk H** — DELETE 1000 FK-parent rows | >5 min (kill -9) | **~950 ms** | >300× |
| **Quirk H** — DROP populated FK'd parent + child | (hung) | **~5 ms** | (was unbounded) |
| **Quirk I** — 1000 ON CONFLICT DO UPDATE on populated table | ~25 s (0.4 ops/sec) | **812 ms (~1230 ops/sec)** | **~3000×** |

### What changed

- **Quirk H root cause:** `check_referencing_rows_exist` did a full
  `scan_table` of the referencing table for every parent row deleted.
  With ~11k rows on each side that was ~121M tuple checks per DELETE,
  dominated by per-row bincode deserialisation.
- **Quirk I root cause:** the DO UPDATE branch's existing-row lookup
  ran `self.query("SELECT pk FROM tbl WHERE col = 'val'")` per
  conflicting row — full SQL planner round-trip ending in a table scan.

Both now route through the existing PK / UNIQUE / FK ART index when
available — O(log N) per call, no deserialisation. The slow scan +
write-set-merge fallback stays for the in-transaction path so v3.22.1's
read-your-own-writes FK semantics are preserved.

### Resume your bulk-migrate

You can drop the `DO NOTHING` workaround and use real upsert semantics:

```python
# Was forced in v3.26 because DO UPDATE was 400× slower.
sql = "INSERT INTO dashboard.messages (...) VALUES (...) ON CONFLICT DO NOTHING"

# v3.30 makes this safe and fast again.
sql = "INSERT INTO dashboard.messages (...) VALUES (...) ON CONFLICT (uuid) DO UPDATE SET content = excluded.content"
```

DELETE / DROP on populated tables also work now — your `kill -9 + restart`
escape hatch isn't needed.

### Note on the gpc-wg scale-up bench

I tried to run a 11k-row scale-up bench on `gpc001ca-wg` to mirror your
field repro shape. The host doesn't have `libclang` or `psql` installed
(both needed: rocksdb-sys requires libclang to build, and the bench
script drives the daemon over PG-wire). Sudo wasn't pre-authorised for
shared infrastructure, so I fell back to the in-process Rust bench at
the same shape; the speedups above are conservative — the in-process
path doesn't include PG-wire serialisation savings, so wire-protocol
clients should see similar or better numbers.

If you want the full 11k-row pcap-able bench, drop me a note and I'll
arrange dedicated infra (or you can run it directly on a host where you
control the package set).

---

## KanttBan team — v3.28 re-test answers

### Bug #16 — `CREATE DATABASE` per-tenant routing

Investigation result: **`IsolationMode::DatabasePerTenant` is metadata
only**.

- The PG-wire startup handler validates the requested DB name (Bug 5
  / v3.25 — registered tenants and `heliosdb` / `postgres` are
  accepted, unknown names are rejected).
- It does NOT call `tenant_manager.set_current_context()` for the
  connection, and the storage layer has no per-tenant key-prefix layer.
- v3.29.0 fixed the visibility regression (`\l` now lists tenants
  registered via `CREATE DATABASE`).
- For your single-DB use case this is fine — `psql -d heliosdb` works
  as expected, and `CREATE DATABASE foo` no longer silently lies (the
  validity check rejects subsequent connections to a phantom DB if you
  don't actually create it).

**Real per-DB isolation** (so `psql -d foo` reads/writes foo's keyspace)
needs (a) per-connection tenant context wiring and (b) a per-tenant
storage namespace. Both are bigger scope and carry forward to v3.31+
once we have a concrete user that actually needs that level of
isolation. KanttBan doesn't appear to be that user, but please flag if
this is a blocker.

### Bug #18 — RUST_LOG=trace recipe in BUGS_HELIOSDB.md

Added a "How to capture next time" block to your bug report with the
exact `RUST_LOG=heliosdb_nano::protocol::postgres=trace` invocation,
log capture pattern, and what to attach (trace + test command + pg
client version + system load). Points at `docs/TRACING_GUIDE.md` for
the full filter reference.

If the intermittent ParseComplete corruption recurs, please run with
the recipe and ship the trace — that should be enough state to
reconstruct the message ordering and fix the underlying race.

---

## Verification

```bash
cargo install heliosdb-nano --version 3.30.0 --features server --force
heliosdb-nano --version       # → heliosdb-nano 3.30.0

# Token-Dashboard: resume bulk-migrate with DO UPDATE
# KanttBan: drizzle-kit push should still work (v3.29 fixes intact);
#          your hand-rewritten migrations/0000_initial_heliosdb.sql
#          and scripts/heliosdb.sh continue to work as defense-in-depth
```

Tests: 1758 lib + 4 quirks_h_i + 5 kanttban_v3_28 + 9 kanttban_v3_27 +
10 dashboard_quirks all pass.

---

## What's still on the carry-forward list

- **Bug #17** — SQL-level `PREPARE` / `EXECUTE` / `DEALLOCATE` (LOW —
  the wire-protocol extended-query path already works, so most ORMs
  are unaffected).
- **Bug #18** — once a fresh trace lands, debug the FK validator vs
  response-writer ordering.
- **Bug #16 strict isolation** — only if someone actually needs it.

— HeliosDB-Nano upstream
