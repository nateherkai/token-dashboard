# HeliosDB-Nano quirks observed by the Token-Dashboard team

This is the running catalogue of HeliosDB-Nano behaviour gaps that surfaced
during the dashboard's integration + cutover work (April–May 2026).

Each entry is labelled A–L, with status against the latest tested binary.
For the in-flight fix status of each upstream item, see:

- Upstream issue **#1** — *Feature request: PyO3 binding for EmbeddedDatabase* — <https://github.com/dimensigon/HDB-HeliosDB-Nano/issues/1>
- Upstream issue **#2** — *Bug: CREATE MATERIALIZED VIEW produces wrong aggregates on v3.30.1* — <https://github.com/dimensigon/HDB-HeliosDB-Nano/issues/2>

Issue body drafts are kept locally at `.github-issue-pyo3.md` and `.github-issue-mv-bug.md`.

Companion docs:
- `HELIOSDB_INTEGRATION_PROPOSALS.md` — the original 10-feature plan.
- `HELIOSDB_INTEGRATION_RESULTS.md` — mirror-mode shipping report (all 10 features live).
- `HELIOSDB_CUTOVER_RESULTS.md` — HeliosDB-as-primary cutover attempt + perf retest.
- `HELIOSDB_v3_30_0_HANDOFF.md` — upstream's handoff doc closing Quirks H + I.

Last tested: **v3.32.1** (built from source at `/home/app/Helios/Nano`,
features `code-graph,graph-rag,mcp-endpoint,code-embed`).

## Status summary

| Quirk | Topic | First seen | Latest status (v3.32.1) | Upstream ticket |
|---|---|---|---|---|
| A | implicit-tx COMMIT visibility | v3.26.0 | not a bug (initial misdiagnosis) | — |
| B | CTE + parameter binding returns zero rows | v3.26.0 | **still broken** | mentioned in #2 side-notes |
| C | `SHOW BRANCHES` empty after `CREATE BRANCH … AS OF NOW` | v3.26.0 | **still broken** | mentioned in #2 side-notes |
| D | `SUBSTRING(s FROM x FOR y)` parser error | v3.26.0 | **fixed in v3.30** | — |
| E | SQL parse rejects payloads w/ ctrl chars or >50k chars | v3.26.0 | **fixed in v3.30** | — |
| F | TCP accept stalls during burst reconnects (>10/s) | v3.26.0 | worked around client-side; not reproducible at the rate the cutover-completed dashboard generates | — |
| G | psql `\d "dashboard.messages"` segfault on composite-name | v3.26.0 | reported fixed in v3.30.1 patches (kanttban-v3_30-retest); not retested | — |
| H | `DELETE` / `DROP` on populated tables hung daemon | v3.26.0 | **fixed in v3.30** (no longer hangs); 200-row DELETE perf still ~140s vs the handoff doc's "~5 ms" claim — possibly different code path | — |
| I | `ON CONFLICT DO UPDATE` ~500× slower than INSERT on populated tables | v3.26.0 | **fixed in v3.30** (690 ops/sec at 424k rows) | — |
| J | `CREATE MATERIALIZED VIEW` produces wrong aggregates | v3.30.1 | **still broken** in v3.32.1 (COUNT(DISTINCT) off by ~60×, COUNT+SUM off by ~1000×, GROUP BY missing rows) | **#2** |
| K | `\d "dashboard.messages"` introspection (alias of G) | reported fixed in v3.30.1 patches | not retested | — |
| L | "Embedded mode" via shipped wheel is subprocess pipe, not in-process Rust → 3-100× slower than SQLite for hot queries | v3.30.0 | architectural — needs PyO3 binding | **#1** |

## Bench numbers — the motivation for #1

Same 4 queries against a 433k-row `dashboard.messages` table, same indexes:

| query | PG-wire (pg8000) | embedded REPL pipe | stdlib `sqlite3` |
|---|---|---|---|
| `COUNT(*)` | 715 ms | ≤ 309 ms* | **1.7 ms** |
| `COUNT(DISTINCT session_id)` | 1,489 ms | ≤ 309 ms* | **27 ms** |
| `WHERE type='user'` | 1,312 ms | ≤ 309 ms* | **76 ms** |
| `GROUP BY project_slug, SUM(...)` | 1,439 ms | ≤ 309 ms* | **145 ms** |

\*REPL number is an upper bound — bench detects query-done via "0.3 s of silence after last output", so the floor is the detector, not the query.

## What the catalogue affects in practice

- **Mirror-mode** (today): dashboard runs on SQLite primary + HeliosDB mirror. All 10 Path-B features are live at <https://td.danielmoya.cv>. None of the quirks block this path.
- **Cutover** (parked): `TD_PRIMARY_STORE=helios` flag exists and works functionally, but PG-wire aggregate latency is 5–30× SQLite's — `/api/prompts` even hits the NPM 60 s proxy timeout. Quirk J (MV) was the planned mitigation; until that lands or a PyO3 binding ships (#1), the cutover stays parked.
- **Semantic search** (live, sub-20ms): HNSW path is the headline HeliosDB win — SQLite can't do this at all. Hash-fallback embedder remains in place because `code_embed` isn't exposed as a SQL function in v3.32.1 (orthogonal HeliosDB-side ask, not yet filed).

## Per-quirk repros + context

### Quirk A — implicit-tx COMMIT visibility

Initially flagged as broken when an INSERT didn't appear visible on a subsequent SELECT without an explicit `COMMIT`. Re-tested carefully against v3.26.0 and v3.30.1: same-session reads see uncommitted writes, cross-session sees them after pg8000's default behaviour. **Not a bug.** The root cause of the initial confusion was pg8000's `autocommit=False` default colliding with HeliosDB's "Transaction already active" gate. Fix: set `conn.autocommit = True` at connect time (done in `token_dashboard/helios_writer.py:_connect_raw`).

### Quirk B — CTE + parameter binding returns zero rows

```sql
-- Direct (works)
SELECT COUNT(*) FROM dashboard.messages WHERE COALESCE(input_tokens,0) >= $1;  -- → 433219

-- CTE form (returns None / empty)
WITH s AS (SELECT COALESCE(input_tokens,0) AS x FROM dashboard.messages)
SELECT COUNT(*) FROM s WHERE x >= $1;  -- → None
```

Workaround in `db_helios.py`: all queries are CTE-free. `alerts.py` rewritten as flat WHERE.

### Quirk C — SHOW BRANCHES empty after CREATE BRANCH

```sql
SHOW BRANCHES;                              -- → empty
CREATE BRANCH 'foo' AS OF NOW;              -- → OK 0
SHOW BRANCHES;                              -- → still empty
```

Branch creation succeeds but isn't enumerable. Workaround: `branches.list_snapshots()` returns `[]` gracefully; UI shows "no snapshots yet".

### Quirk D — SUBSTRING(s FROM x FOR y) parser error → FIXED in v3.30

Was: `Expression not yet supported: Substring { … special: true }`. Function form `SUBSTR(s, x, y)` always worked. Fixed in v3.30 — both forms now accepted.

### Quirk E — bound payload with ctrl chars / >50k chars → FIXED in v3.30

Was: `Unterminated string literal at Line:N, Column:N` even via `$N` binding. Fixed in v3.30 — NUL bytes and 60k-char strings round-trip cleanly. The dashboard's `_sanitize_text` shim stays as defense-in-depth.

### Quirk F — TCP accept stalls during burst reconnects

Closing and re-opening connections faster than ~10/sec caused `_make_socket: recv timed out`. Worked around with exponential backoff (0.5/1/2/4/8s, 5 retries) in `helios_writer._connect_raw`. Not reproducible at the dashboard's steady-state connection rate.

### Quirk G — psql segfault on `\d "dashboard.messages"`

Composite-name table introspection via psql's `\d` crashed with column-out-of-range. Reported fixed in v3.30.1 patches per the changelog ("kanttban-v3_30-retest \d index-list 12-col matcher"). Not retested by us.

### Quirk H — DELETE / DROP on populated tables → FIXED (partial)

Was: 11k-row `DELETE` hung the daemon indefinitely; needed `kill -9` + restart twice. Fixed in v3.30 — no longer hangs. But: 200-row DELETE benched at 140 s, vs the handoff doc's claim of ~5 ms (which was on a FK-shape repro). Workaround: avoid DELETE/DROP on populated tables; use ON CONFLICT DO NOTHING for dedupe.

### Quirk I — ON CONFLICT DO UPDATE 500× slower than INSERT → FIXED in v3.30

Was: 0.4 ops/sec at 11k rows (vs 181/sec bare INSERT). Fixed in v3.30 — **690 ops/sec at 424k rows**. The full SQLite → HeliosDB bulk migration of 427k messages now completes in 7m50s, 100% success rate.

### Quirk J — CREATE MATERIALIZED VIEW wrong aggregates → BUG, FILED AS #2

Critical bug. Full repro in `.github-issue-mv-bug.md` and in upstream #2. Bisection on a 433k-row table:

| MV form | direct query | MV value | bug? |
|---|---|---|---|
| `COUNT(*)` | 433219 | 433219 | ✓ correct |
| `COUNT(DISTINCT session_id)` | 234-265 | 4 | ✗ ~60× off |
| `COUNT(*), SUM(input_tokens)` | 433219 / 2687871 | 407 / 353 | ✗ ~1000× off |
| `GROUP BY type` | 6 types totalling ~430k | 4 types totalling ~412 | ✗ missing types + wrong counts |

`REFRESH MATERIALIZED VIEW` and `REFRESH … INCREMENTALLY` both leave the wrong values; `pg_mv_staleness()` reports `FRESH`. Hypothesis: MV computed against a small slice (memtable / single SST / WAL segment) rather than the full LSM tree.

### Quirk K — `\d` introspection (duplicate of G)

Tracked separately because the v3.30.1 patches mention closing the `\d` family without specifying composite-name behaviour. Will fold back into G when retested.

### Quirk L — "Embedded mode" is subprocess-pipe → FEATURE REQUEST AS #1

Published `heliosdb_sqlite` Python wheel (3.0.1) calls itself "embedded" but per its `heliosdb_sqlite/main.py:761` it spawns `heliosdb-nano repl` as a subprocess and pipes queries via stdin/stdout. Architectural, not a bug — but it means "embedded mode beats SQLite" only holds at the Rust level today. Filed as #1 with the bench table above.

## Carry-forward for future retests

When the next HeliosDB-Nano release lands (currently v3.32.1 is latest), retest in this order:

1. Quirks B, C, J — the still-open ones; one tag bump could close any.
2. Quirk H perf — try the FK-shape DELETE from the handoff doc to see if the ~5ms claim reproduces on our infra.
3. PyO3 binding (#1) — would render Quirk L moot.
4. Pull any new ones into this catalogue as they're discovered.

The dashboard's `scripts/_cutover_validate.py` compares `db.py` (SQLite) vs `db_helios.py` (HeliosDB) on the same queries — run it after each retest to confirm no functional regressions on the read paths.
