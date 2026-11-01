# Non-root deployment — 2026-09-21

Superseded by `SSE-LIMITS.md`; that release retains these runtime controls.

Production runs `token-dashboard:nonroot-20260921`, image ID
`sha256:98fc670ba571db57a150aaf67bd255c15aaef152f6521d57d7b94bc41637411f`.
Canonical deployment is `/home/app/token-dashboard-live/compose.yml`; the source
Dockerfile also has a non-root default for future builds. The release layers
only the OS user/data-directory setup on the prior CSRF image:39 application
code/UI artifacts match. Unrelated source changes, including pending SSE work,
were not copied into the release.

PID1 and exec processes run1001:1001. Existing projects and source mounts remain
read-only; the cache volume remains writable and is now owned1001:1001 with
directory0700/database0600. A metadata-only UID1001 check found all624 existing
project files and324 source files readable, with no directory permission errors.
No corpus contents or source ownership were changed. Root-owned image history
files remain inaccessible to the runtime user; application code is readable.

Private `runtime.env` in the deployment directory is0600, Git-ignored and excluded
from build context. Compose raw env-file parsing was compared to the original
container to preserve exact values without interpolation; no secrets were
printed. Runtime environment, command, restart policy and internal-only network
exposure remain unchanged. Source registry still points to the source repository.

## Verification and limits

- SQLite backup-API copy passed integrity, restore/login and actual isolated
  create/insert/read/rollback checks as1001. Seven existing immutable-image server
  tests pass; pre-existing socket ResourceWarnings remain.
- Candidate and live UID1001 passed a read-only authenticated HeliosDB `SELECT1`
  using the existing configured password. No schema initializer or guessed
  credential was used for this check; the engine/version is unchanged.
- Live PID1 identity, cache write access and internal login pass; zero restarts.
  Configured NPM domain `td.danimoya.com` serves login200 with valid TLS.
  Inventory hostname `td.danielmoya.cv` fails TLS before reaching the app and
  requires a separate repair/retirement decision. No proxy/certificate changes.
- No production login submission or authenticated dashboard workflow was used.
  The isolated rehearsal used no production credentials/corpus and was removed
  with its disposable cache volume after validation.

Private backups are under `/tmp/token-dashboard-nonroot-backup-20260921`.
The backup-API copy was restore-tested; the full cache archive captured after
confirmed stop was catalog-checked. The original container is retained stopped,
network-disconnected and restart-disabled as
`token-dashboard-before-nonroot-20260921`. Do not start it blindly or restore stale
cache data for a user-only rollback. The old image can be run with the same1001
Compose user after the ownership change. Preserve production secrets and the
read-only corpus/source mounts in any reviewed rollback.
