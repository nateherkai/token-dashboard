# HeliosDB-Nano v3.34.0 — Outstanding Issues

From the Token Dashboard integration. Verified against a live v3.34.0 instance
on 2026-05-31 (built from tag `v3.34.0`, features `code-graph,graph-rag,mcp-endpoint,code-embed`),
serving a ~448k-message corpus over the PG wire (pg8000) + HTTP. Access modes:
`psql`/pg8000 on `:5440` (PG wire) and `curl` on `:18090` (HTTP).

## 🎉 v3.37.0 (2026-06-04) — #1 MCP-over-HTTP FIXED → ALL 7 TD issues closed

v3.37.0 ("Token Dashboard MCP-over-HTTP support" release; tag `v3.37.0` / `82949fa`) closes the last
open item, **#1**, in both halves — verified against the running binary:
- **MCP-over-HTTP routes mounted on `--http-port`.** `GET /mcp/info` returns real capabilities
  (`protocolVersion 2024-11-05`, tools+resources). `POST /mcp` `initialize` → `serverInfo
  {name: heliosdb-nano, version: 3.37.0}`; `tools/list` → a real catalog (`heliosdb_query`,
  `heliosdb_schema`, `heliosdb_list_tables`, `heliosdb_create_table`, `heliosdb_insert`, branch ops…).
  `/health` still serves `{"status":"ok"}` on its own route (no longer a catch-all).
- **`CODE_EMBED(text)` SQL function** returns a real 384-dim embedding vector (not the BLAKE2b hash
  fallback) — the dashboard's semantic search / clustering / dup-retrieval can now use real embeddings.
- #2–#7 all held (no regressions): SHOW BRANCHES lists names, CREATE SCHEMA OK, CTE+`$param` → 5.

**ALL 7 original TD issues are now resolved:** #1 MCP-over-HTTP → v3.37.0 · #2 MV `COUNT(*)` →
v3.35.0 · #3 CTE+`$param` → v3.36.0 · #4 `SHOW BRANCHES` → v3.36.2 · #5 `CREATE SCHEMA` → v3.36.0 ·
#6 MV-column `SUM` type → v3.35.0 · #7 cold-start → v3.35.0.

---

## ✅ v3.36.2 (2026-06-04) — TD#4 fixed; #1 MCP-over-HTTP is the ONLY remaining item

v3.36.2 (`caba972`, `main`) fixed **TD#4 SHOW BRANCHES** — verified on the live prod instance: it now
lists named branches (`name|id|parent|ts|status`), including ones older binaries left in the data-dir
(so it's a real planner/projection fix, not a fresh-data-dir artifact). #3/#5 not regressed; data 6/6;
dashboard sub-second. Prod engine is now v3.36.2.

**Status of the original 7 TD issues: 6 fixed (#2, #3, #4, #5, #6, #7); 1 remaining → #1.**

The sole open item is **#1 — MCP routes not mounted on the daemon's `--http-port`** (deferred as a
*feature*, not a bug). Full details + repro + suggested fix are in section **"### 1."** below. In short:
`heliosdb-nano start` serves only a health responder on `--http-port` (every path → `{"status":"ok"}`),
there is no `--mcp` flag and no `mcp` subcommand, and `code_embed` isn't exposed as a SQL function — so
an MCP-over-HTTP client (and real semantic embeddings) have nothing to talk to. **Fix:** mount the MCP
JSON-RPC router (POST `/mcp`) + a `code_embed` SQL function on the `--http-port` listener, or add a
`heliosdb-nano mcp` subcommand.

---

## ✔ v3.36.1 verification (2026-06-03)

Built **v3.36.1** (tag `v3.36.1` / `release: 0b46fc9`) on gpc001gb-wg, deployed to the dashboard's
instance (reads the v3.35.0 data-dir cleanly; cold-start ~12 s). Independently verified the
v3.36.0 claims against the running binary:

| # | Issue | v3.36.0 claim | v3.36.1 verified |
|---|---|---|---|
| 1 | MCP routes on `--http-port` | DEFERRED | ✅ confirmed deferred — still `{"status":"ok"}` health-only; subcommands are only `start`/`stop`/`status` (no `mcp`) |
| 3 | CTE + `$param` | FIXED | ✅ **confirmed FIXED** — `WITH x AS (SELECT 5 AS n) SELECT n FROM x WHERE n>=$1` returns 5; CTE `COUNT(1)` = flat `COUNT(1)` = 104,856 |
| 4 | `SHOW BRANCHES` | FIXED | ❌ **NOT fixed** — see below |
| 5 | `CREATE SCHEMA` | FIXED | ✅ **confirmed FIXED** — `CREATE SCHEMA IF NOT EXISTS` accepted as a no-op; the per-write schema warning is gone |

Held from earlier: #2 `COUNT(*)` over MV, #6 MV-column `SUM`→int, #7 cold-start; real 200-row
DELETE **0.713 s**; live-JOIN `/api/tools` **0.011 s**; data exactness now **6/6** (the live-session
`tools` drift settled).

**#4 `SHOW BRANCHES` — claim contradicted on a fresh data-dir.** The v3.36.0 note attributed the
blank row to "a branch registry persisted by an older binary; re-test on a fresh data dir." Did
exactly that — a brand-new v3.36.1 instance on an empty data-dir, seeded a table + rows, created
two named branches (`'alpha'`, `'beta'`, both `OK 0`):
```
SHOW BRANCHES;
 branches
----------

(1 row)
```
Still **one blank-name row**; `alpha`/`beta` are not listed. `fix(TD#4)` routes `SHOW BRANCHES`
through the planner (it no longer errors) but does not populate the branch names — branches remain
un-enumerable from SQL even on a clean data-dir, so the "old binary registry" explanation doesn't
hold.

**Net across the original 7:** 5 fixed (#2, #3, #5, #6, #7), 1 deferred feature (#1), 1 still-open
despite the FIXED claim (#4).

---

## ✅ v3.36.0 retest (2026-06-02) — the 4 remaining items resolved or deferred

Released as **v3.36.0** (HeliosDB-Nano PR #4, merged to `main`). Re-status of the
items still open after v3.35.0:

| # | Issue | v3.35.0 | v3.36.0 |
|---|---|---|---|
| 1 | MCP routes on `--http-port` | ❌ health-only | ⏳ **DEFERRED** — `--http-port` is a health responder and `McpServer` is stdio/unix-socket only; mounting MCP needs an HTTP+JSON-RPC route (POST `/mcp`) or an `mcp` subcommand (a feature, not a fix) |
| 3 | CTE + `$param` → 0 rows | ❌ 0 rows | ✅ **FIXED** — reproduced over the wire with pg8000; both wire handlers misrouted `WITH … SELECT` (it isn't `SELECT`-prefixed) to the command path. Now routed as row-returning in both handlers |
| 4 | `SHOW BRANCHES` empty | ❌ blank row | ✅ **FIXED** — resolved in the planner so it lists branches through every entry point (a live single blank-name row means a branch registry persisted by an older binary — re-test on a fresh data dir) |
| 5 | `CREATE SCHEMA` unsupported | ❌ error | ✅ **FIXED** — accepted as a flat-namespace no-op |

Items 2, 6, 7 were already closed in v3.35.0. Per-fix detail + regression tests
are in PR #4 and `CHANGELOG.md` `[3.36.0]`; open follow-ups across all three
reports are consolidated in `~/NANO-OPEN-FOLLOWUPS.md`.

---

## v3.35.0 retest (2026-06-02)

Re-ran every item below on **v3.35.0** (built from `release: v3.35.0` / `bea50a7`, same
instance + corpus). **3 of 7 closed; both v3.34.0 wins held.** The Nano team clearly worked
the handoff (commits `fix(T2)`, `test(T3)`, `fix(IFNE)`/`test(T4)`, `fix(T8)`).

| # | Issue | v3.34.0 | v3.35.0 |
|---|---|---|---|
| 1 | MCP routes on `--http-port` | health-only | ❌ still health-only |
| 2 | `COUNT(*)` over MV = 0 | 0 | ✅ **fixed** (1-row MV → 1; 60-row MV → 60) |
| 3 | CTE + `$param` → 0 rows | 0 rows | ❌ still 0 rows (see note) |
| 4 | `SHOW BRANCHES` empty | empty | ❌ still not enumerable (see note) |
| 5 | `CREATE SCHEMA` unsupported | error | ❌ still unsupported |
| 6 | MV-column aggregate type | NULL | ✅ **fixed** (returns int, consistent with base tables) |
| 7 | Cold-start regression | ~27 s | ✅ **fixed** (~9 s, beats v3.33.0's ~11 s) |

Held from v3.34.0: real 200-row DELETE **0.99 s**; live-JOIN `/api/tools` **0.009 s**. No perf
regressions; data 5/6 (the `tools` diff is the live session, not a regression).

**Two discrepancies — fix claimed upstream but the real-world query still fails:**
- **#3 / `51591d2 test(T3) "already-fixed, confirmed"`** — `WITH x AS (…) SELECT … WHERE col >= $1`
  still returns **0 rows** on v3.35.0. Minimal repro: `WITH x AS (SELECT 5 AS n) SELECT n FROM x
  WHERE n >= $1` (bind `$1=0`) returns nothing; the same query without the CTE returns the row.
  The T3 unit test passes but this shape still drops every row.
- **#4 / `8663e3d fix(IFNE)` + `da4c591 test(T4) "records the name"`** — `CREATE BRANCH … AS OF NOW`
  returns OK, but `SHOW BRANCHES` still returns a single empty-name row and there is no
  `_hdb_branches` / `helios_branches` table, so branches still aren't enumerable from SQL.

The four still-open items (1, 3, 4, 5) are detailed below, unchanged.

## Fixed in v3.34.0 (context — these are now off the list)

| Issue | v3.33.0 | v3.34.0 | speedup |
|---|--:|--:|--:|
| Real 200-row `DELETE` on a populated table | ~140 s | **1.05 s** | ~133× |
| Per-statement `DELETE` floor (no-op delete) | ~0.6 s | **~0.000 s** | — |
| Live JOIN route `/api/tools` (`tool_calls`⋈`tool_results`) | 0.586 s | **0.009 s** | ~65× |

These were the two biggest operational blockers for the dashboard cutover. Both
are resolved; the client-side batch-DELETE-per-scan workaround is no longer
strictly necessary (kept for now, harmless).

---

## Outstanding issues

### 1. [HIGH] MCP routes not mounted on the daemon's `--http-port`
The binary is built `--features mcp-endpoint`, but `heliosdb-nano start` serves
only a health responder on `--http-port` — **every path returns `{"status":"ok"}`**,
including a valid MCP JSON-RPC `initialize`/`tools/list`.

Repro:
```
$ curl -s -X POST http://127.0.0.1:18090/mcp \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
{"status":"ok"}
```
`start --help` documents `--http-port` as "HTTP API port for health checks";
there is no `--mcp` flag and no `mcp` subcommand. Related: `code_embed` is still
not exposed as a SQL function, so a client has no way to get real embeddings from
a running daemon.

**Impact:** the dashboard's MCP tab (`catalog`/`verify`/`replay`) has no endpoint
to talk to, and semantic features fall back to a BLAKE2b hash embedder
("plumbing-correct, semantics-poor" — clusters collapse, dup-retrieval finds 0
pairs). **Suggested fix:** mount the MCP router (and a `code_embed` SQL function)
on the `--http-port` listener in `heliosdb-nano start`, or add a `heliosdb-nano mcp`
subcommand.

### 2. [HIGH] `COUNT(*)` over a materialized view returns 0
The MV's data is correct — `SELECT cols …`, `WHERE`, and `SUM(1)` all return the
right rows/counts — but a bare `COUNT(*)` returns 0.

Repro (on an MV with 1 row, 270 sessions of data):
```
SELECT COUNT(*) FROM td_overview;   -- => 0     (wrong)
SELECT SUM(1)   FROM td_overview;   -- => 1     (correct)
```
**Impact:** silent under-count footgun; any code that `COUNT(*)`s an MV gets 0.
Client never `COUNT(*)`s an MV (uses `SUM(1)` / reads rows). **Suggested fix:**
route `COUNT(*)` over an MV through the same path as `SUM(1)`.

### 3. [HIGH] CTE + parameter binding silently returns zero rows
A valid Postgres CTE combined with a `$N` placeholder returns nothing, even when
the underlying data matches.

Repro:
```
WITH x AS (SELECT input_tokens FROM dashboard.messages WHERE type='assistant')
SELECT SUM(1) FROM x WHERE input_tokens >= $1;   -- bound $1=0  => 0 rows / NULL
```
The same query inlined without a CTE returns the correct count. **Impact:**
silently wrong results — the most dangerous failure mode. `db_helios.py` is kept
strictly CTE-free as a workaround. **Suggested fix:** bind parameters through the
CTE plan.

### 4. [MEDIUM] `SHOW BRANCHES` empty after a successful `CREATE BRANCH`
`CREATE BRANCH '<name>' AS OF NOW` returns OK and the branch is usable, but
`SHOW BRANCHES` always returns a single row with an empty branch name.

Repro:
```
CREATE BRANCH 'td-probe34' AS OF NOW;   -- OK
SHOW BRANCHES;                          -- => 1 row: ('')
```
**Impact:** branches exist but aren't enumerable, so the dashboard can't list
snapshots for time-travel. **Suggested fix:** populate `SHOW BRANCHES` from the
branch catalogue (the `list_branches()` surface reportedly already exists in Rust).

### 5. [MEDIUM] `CREATE SCHEMA` not supported
```
CREATE SCHEMA IF NOT EXISTS dashboard;
ERROR XX000: Query execution error: Statement not yet supported:
  CreateSchema { schema_name: Simple(ObjectName([Ident { value: "dashboard" }])) }
```
**Impact:** the dashboard cannot create a real schema; it stores everything in
`public` using composite table names (`dashboard.messages` as a literal table
name). Works, but every write logs a schema warning and the layout is awkward.
**Suggested fix:** support `CREATE SCHEMA` (even as a no-op namespace) so
`schema.table` resolves normally.

### 6. [MEDIUM] MV-column aggregate returns `NULL` (type inconsistency)
pg8000 receives base-table `SUM`/`COUNT` as ints, but **summing an MV's own
columns returns `NULL`** on v3.34.0 (it returned a *string* on v3.33.0 — the
behavior changed, neither matches the base-table int).

Repro:
```
SELECT SUM(input_tokens) FROM dashboard.messages WHERE type='assistant'; -- int
SELECT SUM(input_tokens) FROM td_overview_daily;                         -- NULL
```
**Impact:** ranged aggregates that re-sum a day-grain MV need `COALESCE(...,0)`
and client-side numeric coercion or they 502 in cost math (`tokens * price`).
Dashboard coerces by column name. **Suggested fix:** make MV-column aggregates
return the column's numeric type consistently with base tables.

### 7. [LOW] Cold-start regression: ~11 s → ~27 s
Daemon time-to-first-query on the same 420 MB data-dir grew from ~11 s (v3.33.0)
to ~27 s (v3.34.0). One-time per start; no steady-state impact. Worth a glance in
case it signals extra startup work (index/MV load).

---

## Lower-severity, still relevant (carried from v3.33.0)

- **GROUP-BY-without-aggregate MV doesn't dedupe** — `CREATE MATERIALIZED VIEW x AS
  SELECT a, b FROM t GROUP BY a, b` (no aggregate fn) materializes all base rows
  instead of distinct pairs. Workaround: resolve such lookups from SQLite.
- **JOIN-based MVs read inconsistently** — a self-join MV returned rows via
  `ORDER BY` but `COUNT`=0. Only single-table GROUP BY MVs are used; self-joins
  (`expensive_prompts`) and `mcp_*` rollups delegate to SQLite.

## Test surface used

PG wire on `127.0.0.1:5440` (pg8000, `autocommit=True`); HTTP on `127.0.0.1:18090`;
dashboard at `127.0.0.1:8080`. Corpus: 448,321 messages · 88,048 tool_calls ·
125,775 tool_results. Each item above is reproducible against that instance.
