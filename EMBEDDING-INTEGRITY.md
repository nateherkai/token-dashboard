# Embedding integrity

Deployment superseded by `VECTOR-READ.md` and `compose.vector-read.yml`; all
embedding integrity controls described here remain in the current image.

Current cleanup status: `VECTOR-QUARANTINE.md` records the successful full-data
restore rehearsal and the active journaled production quarantine. Historical
cleanup is in progress, not complete; the finding must remain awaiting verification.

Deployed 2026-09-21 as `token-dashboard:embedding-integrity-20260921`, image
`sha256:ea4c0ec8addd5add008c9fd93c45b78c7a149dbf162e754227e3437124386d22`.

The embedding shim no longer generates hash-based vectors when MCP is unavailable.
It returns `None` for unavailable or malformed responses and caches only validated
provider results. Validation requires 384 finite numeric non-boolean coordinates
with nonzero norm; this validates shape, not the provider's semantic quality.
Coordinates must also fit float32 and remain nonzero after the existing
six-decimal writer serialization; overflow and all-rounded-to-zero vectors are
rejected before persistence.
Cached vectors are copied for callers so mutations cannot pollute the cache.

Scanner message/tool-result writes and document ingestion preserve text/metadata
with a NULL vector when embeddings are unavailable. Bulk migration no longer
counts a missing vector as an embedded message. Query embedding failure returns
503 before invoking vector search. UI no longer advertises a hash fallback and
explicitly warns that old vector provenance remains unverified.

No schema or database-engine migration, existing-vector deletion, re-embedding
job, provider configuration change or automatic historical cleanup is included.
Existing upserts retain their prior replacement semantics: ingesting a row without
an embedding can replace that row's previous vector with NULL. Recovery applies
to new embedding calls; already-scanned records are not automatically rescanned.

## Historical audit — 2026-09-21 wave 44

The prior 20-row no-match samples are invalid evidence: typed `body_vec` returns
OID1000 and pg8000 decodes the values into booleans. `CAST(body_vec AS TEXT)`
returns OID25 and preserves numeric coordinates. One read-only same-row comparison
had 384 false coordinates through the typed path versus 75 nonzero coordinates
through text. This is a wire-type/decoder discrepancy, not persisted zero vectors.
Separate finding `a83c92921df4` tracks affected Python clustering reads.

The corrected bounded read-only text-cast export covers **10,476 message vectors
and 5,274 tool-result vectors**. All **15,750** match the exact prior-image hash
fallback using stored text normalization and six-decimal writer serialization
(absolute comparison tolerance 1e-7). Before/exported/after counts agree for each
table. This is not a transactional snapshot; lower counts than wave43 are
consistent with intervening ingestion's documented NULL replacement behavior.

Private artifacts: `/tmp/token-dashboard-vector-audit-20260921/vectors.jsonl`
(101,296,779 bytes, mode0600, directory0700), `report.json` (aggregates only),
and the exact prior embedder reference. The initial typed-vector export is named
`typed-vector-export.invalid.jsonl` and must not be used for cleanup or restoration.
Valid export SHA256:
`adaed30cbfe53db954cbb43540e57bf78fc406179b45afccbb2e2335b274d2ce`.
Legacy source SHA256:
`c21726d351f6ed8a8ffbc0767bb96c1cfa6de50afffd7028962fc123ceb8cdb2`.

Operator tools under `scripts/`: `export_embedding_audit.py`,
`legacy_embedding_match.py`, `run_embedding_audit.py`. Fourteen tests pass,
including duplicate/incomplete exports, changed counts, float32 storage, and
reference-function errors aborting rather than falsely reporting non-matches.
No source texts, identifiers or vectors are printed in aggregate reports.

The finding remains awaiting verification: a tested scoped backup/restore path
and compare-and-set quarantine/re-embedding of the identified hashes are still
required. The audit export is sensitive evidence, not yet a restore-tested backup.
No historical vectors have been changed by this audit. Do not close based only
on removal of future fallback writes, unit tests or health checks.

Deployment artifacts use `compose.yml` plus `compose.embeddings.yml` under
`/home/app/token-dashboard-live`. Rollback uses `compose.login.yml` and current
cache/credentials, but reintroduces synthetic fallback risk. Review before
rollback; never restore old data solely for a code-only rollback.

## Release validation

64 focused tests and 71 existing immutable-image tests pass. Tests use mocked MCP,
isolated SQLite and real scanner/writer/document/bulk/search call paths: unavailable
embeddings reach SQL as NULL, bulk counts stay honest, and HTTP search returns 503
without a DB search call. Provider-response/caching tests cover error/malformed
responses, recovery, mutation isolation and storage-compatible numeric validation.
Frontend module syntax checks pass. Existing unrelated test socket warnings remain.

Only four reviewed artifacts differ from the login-limits release; pending budget
code remains excluded. Live hashes match the tested image. Internal/public login
GET returns 200 with valid public TLS, configured-auth DB SELECT1 passes, UID1001
and zero restarts verified. Environment/credentials, mounts, ports, network and
command are unchanged. No production query embedding, login submission or corpus
mutation was used for validation.

Private backup `/tmp/token-dashboard-embedding-backup-20260921/cache.tar` was
captured after a confirmed stop and catalog-checked, not restore-tested. Container
metadata is alongside it. Copy private temporary backups to approved durable
storage before cleanup. This cache backup is not a backup of the HeliosDB vector
corpus and is insufficient for future historical-vector mutation work.
