# Historical vector quarantine — 2026-09-21

Finding `977745afed5e` has completed its cleanup and verification gates.

## Verified completion

The original operator exited0 without restart or duplication. All15,750 planned
rows have complete intent/outcome pairs:15,624 full-run changes and126 skips,
including the10 already-applied pilot rows. No pending intents. Fresh corrected
text-cast export finds zero non-null vectors in both tables, with stable counts.

A bounded read-only verifier checks every original identifier against the frozen
snapshot:15,750 present,15,750 source texts byte-identical, zero missing/changed,
15,750 current vectors NULL and zero original vectors remaining. It uses158
parameterized SELECT batches and outputs aggregates only. Source preservation
also accounts for the116 non-pilot skips: their original rows/text remain and
their vectors are now NULL; this is consistent with concurrent normal upserts,
not evidence of failed cleanup.26 quarantine/reconciliation and8 verifier tests
pass, including second-read snapshot mutation and bounded query/result guards.

Dashboard was briefly stopped before derived cleanup. The current3 clusters and
18 members exactly matched their hash-bound, exact-engine tested backup. Guarded
transactional invalidation removed only those derived tables' contents; the
operator records a durable intent and completed receipt. Mocked finalization
tests cover success plus six refusal gates. Same dashboard container/release was
restarted, with unchanged config, equivalent mounts and network IP; zero automatic
restarts. A transient startup502 cleared on the next check; login returns200 over
verified TLS. Configured-auth DB reads prove563,391 messages and178,911 results,
zero vectors, zero clusters/members, and the application cluster listing is empty.
Nineteen embedding/consumer/cluster tests also pass on the actual production image
offline, with synthetic providers. No real embedding-provider capability is claimed.

Final private0600 evidence: `post-reconciliation.json`, `post-vectors.jsonl`,
`post-report.json`, `source-preservation-report.json`, `derived-clear-report.json`,
`post-start-report.json`, plus pre-stop container metadata. All are in the private
evidence directory below. The first post-export attempt failed before opening a
DB connection/output file because its Python path omitted `/app`; a corrected
read-only export then exited0. The write/quarantine job was never restarted.

Production image remains
`sha256:7035afa6ff088eed0e9f68f3649cad9d6ea5c9036d8fd2d2d9ff653765272d24`.
The native engine binary digest remains unchanged. No engine upgrade occurred.
All7 confirmed-terminal operator/check containers were removed after verification;
the operation names and commands below are history, not instructions to rerun.

## Verified backups and rehearsal

Persistent on-host private evidence lives under
`/home/app/token-dashboard-live/private-evidence/20260921-vector-quarantine`.
Directory0700, evidence0600, excluded from Git and every release build context.
This is not an off-host disaster-recovery backup.

`vectors.jsonl` contains the audited15,750 source-text/vector/identifier records,
SHA256 `adaed30cbfe53db954cbb43540e57bf78fc406179b45afccbb2e2335b274d2ce`.
The exact native production binary was tested in an isolated network-none
container: SHA256
`f929bfe46ef35f426e705ed01267b637cd73482d7fd722387e192679814f80d4`.
Full backup load,15,750 conditional quarantines, idempotent repeat,15,750 restores
and source/vector round-trip hashes passed in1495.49seconds. Changed-source,
changed-vector and nonempty-restore guards passed. `rehearsal-report.json` is
the SHA-bound local operator attestation, not a signed external certificate.

`derived-clusters.json` backs up3 clusters and18 members, SHA256
`95ff7f6f9c4f636a82cb5bd4ec3876cd163393a34f54dc7b1be8a0850133be5a`.
Its independent exact-engine rehearsal passed full restore/clear/restore,
changed-backup rejection, nonempty-target rejection and transaction rollback.
Receipt: `derived-rehearsal-report.json`. These are scoped logical backups of
the fields/tables being changed, not full database or engine backups.

## Completed operation history

The ten-row production pilot completed successfully in5.75seconds; public login
remained200 with valid TLS. The full job is container
`td-vector-quarantine-apply-20260921`, ID
`b47660447246529746a8ad81c1f81cee3a6917173b14ed81bcbf6d42519449ea`.
Do not launch another copy or restart merely because a poll times out.

It uses the existing configured password, reviewed172.17.0.1:5440/heliosdb
target, nonroot UID1001, read-only root filesystem, no published ports,
no capabilities, no-new-privileges,1GiB limit and restart=no.
Only the private evidence mount is writable. Neither database engine nor
application release changes for this operation.

The operator validates frozen bounded snapshot bytes, counts, identifiers and
vector types before opening a DB connection. It requires matching complete
rehearsal evidence. Before writes it fsyncs an exclusive frozen backup copy,
then makes it0400. Each update compares original identifier, source text and
text-cast vector; changed rows skip instead of overwriting newer data. Only
`body_vec` changes to NULL. Every intent and result is journaled/fsynced; errors
halt and log only an error type to public container output. A50ms pause follows
each row.16 unit/guard tests pass, including source-file mutation after preflight.

Check Docker state, aggregate container logs and private
`full-journal.jsonl`; `pilot-journal.jsonl` records the first ten changes.
Frozen backup copies are alongside their journals. A pending intent is not proof
of success; reconcile it against the database before any manual recovery.
The first full-job health observation found login200/TLS valid and concurrent
configured-auth SELECT1 completing in0.547seconds. Continue monitoring while the
larger563,391-message/178,911-result production tables are processed.

## Finish gates (all verified above)

Offline structural reconciliation is available in
`scripts/reconcile_vector_quarantine.py SNAPSHOT FULL_JOURNAL --pilot-journal PILOT_JOURNAL`.
It checks the frozen snapshot digest/counts, ordered intent/outcome pairing,
complete full-run footer and pilot overlap, returning only aggregate counts.
Active/incomplete journals fail verification; this does not stop or restart the
operator. Ten synthetic tests include the actual exporter/operator with mocked
connections; all26 quarantine/operator/reconciliation tests pass. This is not a
substitute for database state reconciliation, source preservation or fresh audit.

1. Confirm the SAME full job terminates successfully with a complete journal;
   inspect skipped rows and reconcile all intents. Do not equate batch completion
   from the ten-row pilot with full completion.
2. Export/audit remaining text-cast vectors with the corrected read path. Recheck
   source/row preservation and absence of known synthetic vectors.
3. Pause the dashboard briefly before invalidating derived clusters. Re-read and
   compare against the tested backup; if changed, take and test a new backup.
   Use `derived_cluster_backup.clear_clusters` only against matching data.
4. Restart the same dashboard release and verify login/TLS, configured-auth DB
   reads, empty/valid embedding behavior and absence of stale derived clusters.
5. Only then update Sprinter to done and remove the completed operator container.

Restoring old vectors would reintroduce non-semantic data. Recovery must be
explicitly reviewed, use the immutable backup and applied journal records, and
only restore rows whose source still matches and vector remains NULL. Do not
restore an entire old database to undo these scoped column changes.
