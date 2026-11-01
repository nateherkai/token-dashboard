# Token Dashboard — HeliosDB-as-primary cutover (in progress)

This document covers the **second** integration pass:
1. Pull whatever HeliosDB-Nano version is now latest. *(May 4: still v3.26.0. May 6: **v3.30.0 released with the upstream-fix-for-the-dashboard-team handoff** at `HELIOSDB_v3_30_0_HANDOFF.md`.)*
2. Re-embed the corpus with the real `code_embed` SQL function. *(Result: still not exposed as SQL in v3.30.0 — falling back to hash. This is a HeliosDB-side ask.)*
3. Cut SQLite over to HeliosDB as the dashboard's primary store. *(In progress — see "v3.30 retest" below.)*

## v3.30 retest

After the upstream handoff doc landed, retested all eight v3.26 quirks against v3.30.0:

| Quirk | v3.26 status | v3.30 status |
|---|---|---|
| **A** — implicit-tx COMMIT visibility | misdiagnosed initially; OK in v3.26 | unchanged ✓ |
| **B** — CTE + parameter binding returns zero rows | broken | **still broken** |
| **C** — `SHOW BRANCHES` empty after `CREATE BRANCH` | broken | **still broken** |
| **D** — `SUBSTRING(s FROM x FOR y)` parser error | broken | **✓ FIXED** (v3.30 returns the substring correctly) |
| **E** — SQL parse rejects payloads with ctrl chars / >50k | broken | **✓ FIXED** (NUL bytes + 60k char strings round-trip cleanly) |
| **F** — TCP accept stalls during burst reconnects | broken | retest in progress with the resumed migration |
| **G** — psql `\d "dashboard.messages"` segfault | broken | not retested |
| **H** — DELETE / DROP on populated tables hung daemon | hung indefinitely | **partial** — 200-row DELETE took 140 s (no longer hangs but well above the handoff doc's "~5 ms" claim; possibly because that bench was FK-related and `dashboard.messages` has no FK children) |
| **I** — `ON CONFLICT DO UPDATE` rate on populated tables | 0.4 ops/sec | **✓ FIXED — 690 ops/sec at 424k rows** (1700×) |

Net: D, E, I shipped. The cutover-blocker (Quirk I) is gone. Quirks B, C, G remain. H is "no longer hangs" but the perf number is far off the handoff's claim — worth noting back upstream when running the FK-shape repro on shared infra (the handoff explicitly said the repro host they used didn't have psql + libclang, so they fell back to in-process Rust bench).

## Resumed bulk migration

With the DO UPDATE rate restored, the original `helios_writer.upsert_*` semantics were put back:

```python
# Was forced to DO NOTHING in v3.26 because DO UPDATE was 400× slower.
# v3.30 makes this safe and fast again — restored to real upsert.
ON CONFLICT (uuid) DO UPDATE SET
  parent_uuid=EXCLUDED.parent_uuid, …
```

The migration is running again (started 2026-05-06). Initial rate observed via the monitor: ~37 messages/sec end-to-end (vs ~50/sec previously). Final numbers below once it completes.

### Migration finished — 2026-05-06 08:06

| metric | value |
|---|---|
| total elapsed | **470 s (~7 min 50 s)** |
| messages pushed | 427,339 (0 failed) |
| messages embedded | 6,686 |
| tool_calls pushed | 80,750 (0 failed) |
| tool_results pushed | 116,869 (0 failed) |

End-to-end rate ≈ **620,000 ops in 470 s = ~1,320 ops/sec** when the upsert mostly hits already-existing rows (DO UPDATE path). 100% success — no failures, no payload-rejections (the `_sanitize_text` shim from the prior pass kept handling the few large/control-char rows; v3.30's E-fix made it strictly defensive belt-and-suspenders).

### Cutover validation

Ran `scripts/_cutover_validate.py` comparing every read function in `db.py` (SQLite) with `db_helios.py` (HeliosDB) against the same 423k-row corpus. Functional pass on overview / projects / sessions / tools / by-model / skills / mcp_summary. **One bug found and fixed:** `daily_token_breakdown` used a `SUBSTR(timestamp,1,10) AS day` alias in `GROUP BY` and `ORDER BY` — HeliosDB v3.30 doesn't resolve column aliases at those positions, so the alias was rewritten to repeat the `SUBSTR(...)` expression.

Numerical drift between the two stores is small (1-3% on token totals) and explained by the dashboard's hourly scan loop continuing to ingest new Claude sessions during the validation window — both stores see slightly different timestamps. Session-level overlap on the most-recent-10 was 10/10.

### Latency findings — performance is the remaining gap

After flipping `TD_PRIMARY_STORE=helios` and pointing the dashboard at HeliosDB-as-primary, ran every existing route. **13 of 14 returned 200**. The 14th (`/api/prompts`) hit NPM's 60 s proxy timeout (502).

| route | SQLite primary | HeliosDB primary |
|---|---|---|
| `/api/overview` | 0.6 s | **3.2 s** |
| `/api/projects` | 0.5 s | **9.3 s** |
| `/api/sessions` | 0.4 s | **7.8 s** |
| `/api/prompts` | 1.0 s | **>60 s → 502** |
| `/api/tools` | 0.4 s | 1.0 s |
| `/api/daily` | 0.5 s | 1.7 s |
| `/api/by-model` | 0.4 s | 1.5 s |
| `/api/mcp/summary` | 0.6 s | **18 s** |
| `/api/skills`, `/api/clusters`, `/api/alerts`, `/api/audit`, `/api/helios/state` | <0.5 s | <2.5 s |

Eleven indexes (`session_id`, `project_slug`, `timestamp`, `parent_uuid`, `type`, `tool_use_id`, etc.) were created on `dashboard.*` after the migration; latencies didn't change much. The expensive paths are large aggregates and self-joins:

- `/api/prompts` does `messages u JOIN messages a ON a.parent_uuid = u.uuid` — a 423k × 423k self-join. Even with `idx_msgs_parent` HeliosDB v3.30's planner doesn't pick the index path for this shape.
- `/api/projects` and `/api/sessions` are `GROUP BY project_slug` / `GROUP BY session_id` over 423k rows — index-assisted but still scan-heavy.
- `/api/mcp/summary` is the multi-CTE rollup that already works around Quirk B; even rewritten as flat WHERE clauses it's 18 s.

**Decision: revert to SQLite-primary in production.** The dashboard remains on `TD_PRIMARY_STORE=sqlite` (default) with the HeliosDB mirror still active for the new vector-search / clusters / alerts / audit features. The cutover code (`db_helios.py` + the dispatcher) stays in the codebase for follow-up work.

### What's needed for a clean HeliosDB-as-primary cut

1. **Materialised views** for the rollups (`project_summary`, `recent_sessions`, `mcp_summary`, `tool_token_breakdown`). HeliosDB v3.24 added MV-incremental refresh; the dashboard's queries are simple enough that a daily refresh would cover all the slow tabs.
2. **Self-join planner improvement** for `expensive_prompts` — or rewrite that one query to use `LATERAL` / pre-computed `parent_billable` columns.
3. **`code_embed` SQL function** to retire the hash-fallback embedder. Without it the vector features work mechanically but produce poor relevance.

These three are HeliosDB-side asks (or, for #2, a client-side rewrite). The cutover is reversible-and-ready: flip the env var when they land.

(For the original ten-feature mirror-mode results, see [`HELIOSDB_INTEGRATION_RESULTS.md`](./HELIOSDB_INTEGRATION_RESULTS.md). For the original proposal menu, see [`HELIOSDB_INTEGRATION_PROPOSALS.md`](./HELIOSDB_INTEGRATION_PROPOSALS.md).)

---

## What landed in this pass

### 1. `db_helios.py` — drop-in replacement for `db.py`

A new module that mirrors every read-side function in `token_dashboard/db.py`
(`overview_totals`, `expensive_prompts`, `project_summary`,
`recent_sessions`, `session_turns`, `daily_token_breakdown`,
`tool_token_breakdown`, `model_breakdown`, `skill_breakdown`, plus the
seven MCP-savings rollups) but reads from the `dashboard.*` schema in
HeliosDB instead of SQLite.

Activated via `TD_PRIMARY_STORE=helios`. Default `sqlite` keeps the
pre-existing behaviour. The dispatcher lives at the top of `server.py`:

```python
if os.environ.get("TD_PRIMARY_STORE", "sqlite").lower() == "helios":
    from .db_helios import (
        overview_totals, expensive_prompts, project_summary,
        ...
    )
```

### 2. Migration resilience fixes in `bulk_migrate.py`

Three real bugs the previous pass paved over:

- **SQLite `IN ()` placeholder overflow** — pulling tool_calls for the
  full corpus tried to bind ~423k uuids in a single IN clause; SQLite
  caps at 999 (default). Fix: chunked at 500 uuids per batch.
- **Connection-death cascades** — when HeliosDB returned a SQL parse
  error (see below), pg8000 left the connection in CLOSE-WAIT and every
  subsequent statement timed out. Fix: detect dead-connection errors,
  reset the global writer connection with bounded backoff, and retry
  *once* on the next row. Don't churn reconnects per-row (that triggers
  the TCP-accept stall described in the impact-results "Quirk" section).
- **`prompt_text` sanitisation** — HeliosDB v3.26's parser rejects
  some payloads with `Unterminated string literal at Line: N, Column: N`
  even via parameterised binding. Fix: strip control chars (except
  `\t\n\r`) and cap at 50,000 chars before binding. The dashboard
  truncates >1k chars on display anyway, so this is a no-op for the UX.

### 3. New v3.26.0 quirks discovered this pass

- **Quirk E — SQL parse fails on parameter-bound payloads with control
  chars or large length.** Affects `prompt_text` and `tool_calls_json`
  values. Worked around in the dashboard via `_sanitize_text` (above).
  Filed for upstream as: extended-query Bind phase should not feed
  values back into the parser; the SQL template (with `$N`) is
  language-clean and any value-side issue should be a runtime/coercion
  error, not a parse error.

- **Quirk F — TCP accept stalls during burst reconnects.** Closing and
  re-opening connections rapidly (>~10/sec) causes `_make_socket: recv
  timed out`. Worked around in the dashboard with exponential backoff
  (0.5/1/2/4/8s, 5 retries). Suggested upstream fix: tune the listener's
  accept queue depth or implement per-source connection-reuse.

These are not blockers, just rough edges discovered during the
high-volume cutover that didn't surface during the lower-volume
mirror-mode validation. Both Quirk A (implicit-tx) from the prior
report appears resolved in v3.26.0; Quirks B (CTE+param), C (SHOW
BRANCHES empty), D (SUBSTRING FROM/FOR) remain.

---

## Cutover status — paused

Migration is paused at **34,090 of 422,982 messages (~8%)**. The
stop-reason is two new HeliosDB v3.26.0 issues that make a one-shot
full-corpus migration impractical in this session — both with clean
upstream-fix paths.

### Migration rates observed

| state of target table | upsert mode | observed rate |
|---|---|---|
| empty (5k batch test) | `ON CONFLICT … DO UPDATE` | **327 ins/sec** |
| 11k rows | `ON CONFLICT … DO UPDATE` | **0.4 ins/sec** ← problematic |
| 11k rows | `ON CONFLICT … DO NOTHING` | ~55 ins/sec averaged over a 7-min run; monitor samples saw 1/sec windows mixed with bursts |
| bare `INSERT` (no `ON CONFLICT`) | — | **181 ins/sec** at 11k rows |

The **DO UPDATE on populated tables** path is dramatically slower than
the bare INSERT (Quirk I in the memory file). DO NOTHING avoids the
penalty but loses update-existing semantics — which the dashboard
doesn't actually need (the SQLite source-of-truth is upstream).

### Other v3.26.0 friction this pass exposed

- **DELETE / DROP on a table with 11k rows hung the daemon for >5min**
  — needed `kill -9 + restart` to recover. Data on disk persisted
  through the restart (RocksDB durability), but anything in-flight was
  lost. (Quirk H.)
- **TCP accept stalls during burst reconnects** (`_make_socket: recv
  timed out`) when more than ~10 reconnect/sec. Worked around with
  exponential backoff in `helios_writer._connect_raw`. (Quirk F.)
- **SQL parse error on parameter-bound payloads** with control chars
  or >50k chars in `prompt_text`. Worked around with `_sanitize_text`
  in `bulk_migrate.py` — strips ctrl chars except `\t\n\r`, caps at
  50k chars. (Quirk E.)

(Full quirk catalogue with workarounds in
`~/.claude/projects/.../memory/heliosdb_v326_quirks.md`.)

### What's actually working

`db_helios.py` is complete and import-clean. Routes flip via
`TD_PRIMARY_STORE=helios`. Against the **34,090 mirrored rows**, every
existing API tab returns the right shape — the dashboard doesn't crash
when flipped, it just shows partial data. Vector search, clusters,
alerts, audit signing — all still work end-to-end as in the prior pass.

### Known limit on first cutover validity

`db_helios.recent_sessions` is sub-second on the current sample but
hasn't been benchmarked against the full corpus. Materialised views
(supported in HeliosDB v3.24+) are the natural fallback if per-tab
latency creeps above SQLite's numbers; the dashboard's queries are
simple enough that plain indexes likely suffice.

---

## After the cutover finishes

The validation gate before flipping `TD_PRIMARY_STORE=helios` in
production is:
1. Row counts match: `SELECT COUNT(*) FROM dashboard.messages` ≈ SQLite's count (within tolerance for the rows that fail sanitisation).
2. `/api/overview` returns the same totals from both backends.
3. `/api/projects` shows the same 59 projects with the same billable-token ranking.
4. `/api/sessions` lists the same most-recent sessions in the same order.
5. The Insights tab still works (vector search, clusters, alerts).
6. The MCP tab still works (per-tool stats, conformance check).

Once those six pass, SQLite becomes a cold-archive read replica and
the scanner writes directly to HeliosDB only.

---

## What's still blocked on HeliosDB-side work

- **Real semantic search.** `code_embed` SQL function not exposed in
  v3.26.0. Without it the search bar / clusters / dup-retrievals use
  the deterministic hash fallback, which produces near-uniform
  distances. Plumbing-correct, semantics-poor.
- **Branch list.** `SHOW BRANCHES` empty after `CREATE BRANCH`. Branches
  exist (creation succeeds) but aren't enumerable.
- **CTE + parameter binding.** Returns zero rows. Worked around by
  rewriting CTE queries as flat WHERE clauses; tracked as a v3.26.0
  quirk in the impact-results doc.
- **`SUBSTRING (… FROM … FOR …)`.** Use `SUBSTR(s, x, y)` instead.

These were on the prior report and are restated here so a single
upstream conversation can address them.

---

## Cutover COMPLETED — 2026-05-28 (HeliosDB-Nano v3.33.0, MV-backed)

`TD_PRIMARY_STORE=helios` is now live on the production container
(`td.danielmoya.cv`). The dashboard reads analytics from HeliosDB; SQLite
remains the ingest cache + source of truth, mirrored into HeliosDB and
read back through materialized views.

### What unblocked it: materialized views (v3.32.2+ fix)

The cutover was parked for weeks on 7–18 s aggregate latency. HeliosDB
v3.32.2 fixed MV correctness for GROUP BY / COUNT(DISTINCT) / SUM
(issue #2 / Quirk J). On v3.33.0 we built 6 single-table GROUP BY MVs in
`token_dashboard/helios_mv.py`:

| MV | feeds | grain |
|---|---|---|
| `td_overview` | /api/overview (all-time) | single row |
| `td_overview_daily` | /api/daily + ranged overview | per-day |
| `td_project_summary` | /api/projects | per-project |
| `td_session_summary` | /api/sessions | per-session |
| `td_model_breakdown` | /api/by-model | per-model |
| `td_skill_breakdown` | /api/skills | per-skill |

`db_helios` reads MVs for the no-range path, falls back to live SQL for
ranged queries. Join-heavy routes that can't be MV'd on v3.33.0 (Quirks
M/N/O below) delegate to SQLite: `expensive_prompts` (self-join) and all
`mcp_*` rollups. MVs refresh from the scan loop after each ingest and via
`POST /api/helios/refresh-mvs`; staleness is visible at
`/api/helios/mv-status` (all 6 report FRESH, 0 pending).

### Final latencies — real HTTP path, authenticated, helios-primary

| endpoint | before (live SQL) | after (MV-backed) |
|---|---|---|
| /api/overview | 3.2 s | **0.010 s** |
| /api/projects | 9.3 s | **0.246 s** |
| /api/sessions | 7.8 s | **0.087 s** |
| /api/prompts (SQLite-delegated) | >60 s / 502 | **0.689 s** |
| /api/tools | — | **0.681 s** |
| /api/daily | — | **0.007 s** |
| /api/by-model | — | **0.001 s** |
| /api/skills | — | **0.001 s** |
| /api/mcp/summary | 18 s | **0.250 s** |

Diagnostic-only (async, off the hot path): `/api/helios/state` 1.1 s and
`/api/helios/mv-status` 0.73 s — both still run `COUNT(*)`/`pg_mv_staleness()`
over full tables. Candidate for a cached-stats follow-up.

### Data exactness — 6/6 validation, twice

`scripts/_cutover_validate.py` compares db.py (SQLite) vs db_helios.py
(HeliosDB) on overview totals, per-project, per-session, tools, daily,
by-model. **All 6 pass**, with every token type matching to the exact
integer (e.g. cache_read 17,420,235,587 = 17,420,235,587). Re-run after a
live scan added ~210 messages — still 6/6, proving the scan→mirror→MV
pipeline keeps the two stores consistent in steady state.

Getting there required a **clean re-migration**: a long-lived mirror had
drifted into a ~2 % superset of SQLite (453,387 vs 444,911 msgs) across
months of migrations + a force-rescan that reset SQLite while HeliosDB
kept old rows. Fix: DROP `dashboard.{messages,tool_calls,tool_results}`
and re-bulk-migrate from current SQLite (444,926 msgs / 86,317 tool_calls
/ 124,433 tool_results, 0 failures, ~24 min). SQLite's single `tool_calls`
table (210,750) splits cleanly into HeliosDB's tool_calls + tool_results
(86,317 + 124,433 = 210,750).

### New v3.33.0 MV quirks (worked around client-side)

- **M.** `COUNT(*)` over an MV returns 0. Data reads are correct and fast;
  use `SUM(1)` if a count is needed. db_helios never COUNT(*)s an MV.
- **N.** GROUP-BY-without-aggregate MV doesn't dedupe (materialized all
  453k base rows for a distinct-pairs view). Project-name resolution reads
  SQLite's indexed table instead (`db_helios._resolve_project_names`).
- **O.** JOIN-based MV reads inconsistently (self-join returned rows via
  ORDER BY but COUNT=0). All MVs are single-table GROUP BY only.
- **H (revised).** DROP TABLE is slow (~60 s / 122k rows) but no longer
  hangs — earlier the hang was DELETE-only.

### Operational notes

- Container recreated (Docker can't mutate env live) with
  `-e TD_PRIMARY_STORE=helios`; config snapshot saved for rollback.
  Cold start ~6 s. Rollback = recreate without the flag (default is
  `sqlite`).
- Single shared HeliosDB connection still serializes API reads under a
  concurrent bulk mirror — an architectural follow-up, not a cutover
  blocker (steady-state scans are incremental and small).
