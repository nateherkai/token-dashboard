# Login failure limits

Deployment superseded by `EMBEDDING-INTEGRITY.md` and `compose.embeddings.yml`;
all login controls described here remain in the current image.

Deployed 2026-09-21 as `token-dashboard:login-limits-20260921`, image
`sha256:dfdb0ad47ac005a1d0d3d754e2152e2fde8b5b6edd0b6219513cfd35f898fb2e`.

Release artifacts: `/home/app/token-dashboard-live/Dockerfile.login` and
`compose.yml` plus `compose.login.yml`, layered onto the verified lifecycle image.
Only the reviewed server artifact and limiter module change. Pending budget code
is excluded; dependencies, database engine, runtime credentials and schema do not
change.

Each server process tracks failure/reservation counts independently for the
socket peer and submitted username. Five failures temporarily block new credential
checks for 60 seconds. Subsequent failed windows increase the delay exponentially,
capped at 900 seconds; blocked requests do not extend that deadline. Idle state
expires after 1800 seconds. Successful authentication resets the relevant failure
history without dropping other in-flight reservations. Authentication is reserved
atomically before checking credentials, including concurrent requests.

Blocked logins return generic HTTP 429, `Retry-After`, and `Cache-Control: no-store`.
State is bounded to 2048 hashed identities and refuses new admissions when full
rather than discarding active protection. Admitted credential failures log hashed
peer/account identifiers to stderr, never submitted names, passwords or cookies.
Signed login CSRF verification still precedes credential admission. Existing
authenticated sessions, logout and CSRF protections retain their behavior.

Forwarded IP headers are deliberately ignored. Behind Nginx Proxy Manager, all
clients share the proxy peer bucket, in addition to the account bucket. This is a
conservative anti-spoofing policy, not distinct end-client attribution. An attacker
can temporarily block new logins; this bounded availability tradeoff is inherent
in account lockouts. Existing signed sessions remain usable. Never enable generic
trust of incoming `X-Forwarded-For` to work around a lockout.

State is process-local and resets on restart; replicas would need a shared
limiter or trusted edge policy. No provider/account secrets are rotated here.
These controls are not password hashing, MFA, revocable sessions or audit-log
retention. Separate findings remain pending for those requirements.

Validation must use isolated synthetic credentials/clocks and the actual patched
handler, not production login attempts. Production smoke checks are limited to
login GET, runtime artifacts and existing configured-auth read-only DB access.

Rollback to `compose.lifecycle.yml` with the same current cache and credentials
removes login throttling. It does not require restoring old database contents.

## Verified release evidence

- 53 focused tests and 71 existing immutable-image tests pass. Coverage includes
  actual HTTP 429/Retry-After without credential evaluation, forged-forwarding
  headers ignored in isolated tests, successful recovery after expiry, existing
  session continuity, CSRF rejection, sanitized logs, concurrent reservations,
  independent identity buckets, capacity limits and repeated capped backoff.
- Parent review corrected premature locked-bucket eviction and an escalation-cap
  reset, and bounded escalation arithmetic. Worker tests run in stdlib discovery.
- Across 40 application/frontend artifacts only server and limiter differ. Live
  hashes match tested artifacts. Pending budgets remain excluded.
- Production login GET internal/public returns 200, public TLS validates, existing
  configured-password DB SELECT1 passes, UID1001 and zero restarts confirmed.
  Runtime environment, mounts, command, network and ports are unchanged. Initial
  metadata comparison rejected mount ordering; destination-keyed comparison
  confirmed equivalence, without another restart or mutation.
- No production login submission, load test or lockout simulation occurred.
  Existing unrelated test socket ResourceWarnings remain.
- `/tmp/token-dashboard-login-backup-20260921/cache.tar` was captured after a
  confirmed stop and catalog-checked, not restore-tested. Container metadata is
  alongside it. These private temporary artifacts require approved durable
  backup storage before cleanup. No schema migration occurred.
