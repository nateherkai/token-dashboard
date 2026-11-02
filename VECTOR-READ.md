# Vector read compatibility fix — 2026-09-21

Deployed `token-dashboard:vector-read-20260921`, image
`sha256:7035afa6ff088eed0e9f68f3649cad9d6ea5c9036d8fd2d2d9ff653765272d24`.
Canonical deployment: `/home/app/token-dashboard-live/compose.yml` plus
`compose.vector-read.yml`. Only the reviewed clustering module changes.

The current native Nano3.58.1 service returns typed VECTOR values with OID1000,
which pg8000 interprets as booleans. Both clustering query branches now request
`CAST(body_vec AS TEXT)` and validate JSON numeric coordinates before clustering.
Wrong dimensions, booleans, quoted numerics, nonfinite or unrepresentable values,
and zero-norm vectors reject before any DELETE/INSERT. Error text does not include
source content or identifiers. Valid fractional coordinates are preserved.

72 focused and71 existing immutable-image tests pass. Coverage includes both SQL
branches, invalid-input nonmutation, a valid clustering calculation and oversized
integer rejection. A configured-auth read-only live query verifies that the
deployed parser preserves text-cast coordinates exactly and rejects incorrectly
typed boolean values. No production clustering build ran.

Nonroot UID1001, unchanged environment/credentials/mounts/ports/network/command,
matching live artifact hash, internal/public login200, validTLS and zero restarts
verified. Initial readiness polling expired while the synchronous startup scan
was still running; the SAME container subsequently passed, without restart.
The database binary/service was not changed, and the upstream OID defect remains.
This closes the application read-path finding, not the engine bug itself.

Historical hash embeddings and existing derived clusters are not repaired by
this change. Their cleanup remains under `977745afed5e`. The vector quarantine
rehearsal is independent and runs in a network-none container with the exact
production binary, SHA256
`f929bfe46ef35f426e705ed01267b637cd73482d7fd722387e192679814f80d4`.
Its full restore/quarantine/restore cycle must succeed before production mutation.

Private stopped-cache backup:
`/tmp/token-dashboard-vector-read-backup-20260921/cache.tar`, catalog checked only.
It is not a backup of the vector corpus. Rollback uses `compose.embeddings.yml`
with current data/credentials and reintroduces incorrect typed-vector decoding.
Do not restore stale database contents for a code-only rollback.
