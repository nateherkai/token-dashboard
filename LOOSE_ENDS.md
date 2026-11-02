# Loose Ends & What's Missing

_Status doc for the Token Dashboard + HeliosDB-Nano integration. As of 2026-06-06._

**Bottom line:** the project is **production-ready and live** at `https://td.danimoya.com` — HeliosDB-Nano
v3.37.3 primary read store (MV-backed), in-app auth, MCP tab live (token-gated), systemd-managed, no
socat. All 8 upstream issues (TD#1–#7 + TD#8) are fixed and deployed. The items below are open
threads, optional improvements, and known non-issues — **none are blocking**.

---

## 1. Open / in-flight (external — not ours to finish)

- **Upstream PR #23** — `https://github.com/nateherkai/token-dashboard/pull/23` is **OPEN**, awaiting
  Nate Herk's review. It's framed as a no-pressure demo fork (the HeliosDB integration is a
  private-deployment fork, not a clean upstream contribution), and the outreach email has been sent.
  Nothing actionable on our side until/unless Nate responds. The code lives on the fork
  `danimoya/token-dashboard @ helios-cutover`.

## 2. Missing features (optional, not done)

- **Real embeddings (`CODE_EMBED`) are NOT wired in** — semantic features (search, clustering,
  duplicate-retrieval) run on the **BLAKE2b hash fallback** = "plumbing-correct, semantics-poor"
  (clusters collapse, dup-retrieval finds ~0 real pairs). Verified 2026-06-06: a test embed returns
  hash-shaped output, not a real vector.
  - **Root cause:** `token_dashboard/embedder.py` calls an MCP tool literally named **`code_embed`**,
    which **does not exist** in HeliosDB's MCP catalog. The catalog exposes `heliosdb_embed_and_store`
    (+ 26 others), and v3.37.x exposes the **SQL function `CODE_EMBED(text)`** (verified: returns a real
    384-dim vector over the PG wire). The MCP call fails → silent hash fallback.
  - **Fix:** rewire `embedder.py` to call the SQL `CODE_EMBED(text)` over pg8000 (via
    `helios_writer.get_conn()`) instead of the non-existent `code_embed` MCP tool. One wrinkle: pg8000
    mis-decodes the `VECTOR` return type (shows booleans), so the result needs parsing from its text
    form / a type handler. Then **re-embed historical rows** — existing `body_vec` values in HeliosDB
    were written with the hash fallback, so a re-embed/backfill is needed to convert them.
  - Effort: small–moderate. Until done, the vector-backed Insights features are cosmetic.

## 3. Operational follow-ups

- **Post-restart mirror backfill is manual.** Every HeliosDB engine restart leaves HeliosDB a few
  hundred rows **behind** SQLite (rows ingested during the down-window aren't mirrored — the scanner's
  sink is best-effort and advances its byte-offset past them; this is a *subset*, so
  `reconcile_orphans` finds nothing to drop). Symptom: `_cutover_validate.py` shows `overview`/`projects`
  token sums low. **Current remedy (manual):** run the idempotent backfill after each restart —
  `docker exec token-dashboard python3 /sources/token-dashboard/scripts/_full_migrate.py` (DO UPDATE,
  ~8–10 min) then refresh MVs. **Improvement:** automate it (systemd `ExecStartPost`, or — better — give
  the scanner a reconcile pass that re-mirrors SQLite rows missing from HeliosDB, closing the
  architectural gap that the mirror never backfills).

## 4. Scheduled / hands-off (no action needed)

- **`td.danielmoya.cv` alias removal — 2026-10-25.** The dashboard was migrated to `td.danimoya.com`
  (NPM host 72, SAN cert 152) on 2026-06-06; `.cv` is kept as a transitional alias. The host-wide `.cv`
  cleanup is already scheduled (`~/scripts/npm-remove-cv-aliases.py`, cron `13 4 25 10 *`). Nothing to do
  unless you want `.cv` dropped for this host sooner.

## 5. Known non-issues (don't mistake these for bugs)

- **Data validation reads 5/6 on a live, actively-used dashboard.** The one failing check is `tools`,
  a live (non-MV) JOIN — when the dashboard is in active use, SQLite ingests tool calls slightly ahead
  of the 30 s mirror cycle. **Completed sessions match SQLite exactly**; it reads 6/6 when the session
  is quiet. Not a regression.
- **Internal `HELIOSDB_*.md` docs are untracked (intentional).** The cutover results, quirks catalogue,
  TD#8 spec, and this file are kept out of git (matching the repo's "remove internal planning docs"
  posture). They live in the working tree + on `gpc001ca:~/HDB`.
- **The HeliosDB integration is a private-deployment fork**, not in upstream `main`. It exists only on
  the `helios-cutover` branch / PR #23. The stdlib-only default path is unchanged when
  `TD_PRIMARY_STORE` is unset.

## Reference — current prod topology

- Engine: **HeliosDB-Nano v3.37.3** (`heliosdb-nano.v3373`), built from the git tag (not on crates.io),
  features `code-graph,graph-rag,mcp-endpoint,code-embed`.
- Bind: **`--listen 172.17.0.1` + `--http-listen 172.17.0.1`** → PG `:5440` + HTTP/MCP `:18090`,
  **bridge-only** (off the public interface). MCP is Bearer-token-gated (`--mcp-token`; container
  `MCP_BEARER`). **No socat.**
- Process: **systemd** `td-heliosdb.service` (enabled, `Restart=always`); upgrade = repoint the
  `heliosdb-current` symlink + `systemctl restart td-heliosdb`.
- App: container `token-dashboard` on `management-network`, `TD_PRIMARY_STORE=helios`, SQLite still the
  ingest source of truth, mirrored into HeliosDB, read back via 6 materialized views.
- Rollback chain: v3.37.0 / v3.36.2 / v3.36.1 / v3.35.0 binaries + `data.*.bak` snapshots preserved.
