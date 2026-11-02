# Bounded event streams — 2026-09-21

Superseded by `REQUEST-LIFECYCLE.md`; that release retains these SSE controls
and adds the previously pending HTTP-worker/scan/connection limits.

Deployed `token-dashboard:sse-20260921`, image ID
`sha256:1d97888a22174d255f27cd7bd044bd1b85218a4de5bf6a5e1b7632cd982649e2`.
Canonical deployment uses `/home/app/token-dashboard-live/compose.yml` plus
`compose.sse.yml`. The reviewed `server.sse.py` release artifact excludes
unrelated pending budget routes/imports retained in the source worktree. No
other application files, dependencies, secrets or schema changed.

`/api/stream` now admits at most32 active streams per handler/server instance and
four per verified session. The session key is an opaque hash of the verified
cookie, not its username; distinct logins do not accidentally share a user-wide
bucket. Trusted local open mode shares one four-stream bucket. Excess requests
return429. No cookie/session tokens are logged by the limiter.

Streams expire after300 seconds. Queue waits respect remaining time; socket
writes have at most five seconds or the smaller remaining lifetime. Deadline is
rechecked after queue waiting. A `finally` block releases the slot and closes the
connection on expiry, write failure, disconnect or unexpected exception. Without
connection closure, an expired HTTP keep-alive thread could otherwise remain
waiting after releasing its slot. Browser EventSource clients can reconnect.

Validation:24 focused server tests and71 existing immutable-image tests pass.
Coverage includes budgets/release, distinct session identities, actual EOF at a
shortened lifetime, stalled-write timeout/error cleanup, unexpected exceptions,
post-wait deadline checks and429 admission. Tests use synthetic local sessions,
mocked clocks/errors and isolated sockets, never production stream traffic.
Pre-existing test socket ResourceWarnings remain. Candidate artifact hash matches
the tested release, isolated nonroot startup/login passes.

Production imports confirm the limits; UID1001, configured-auth read-only
HeliosDB SELECT1 and internal/public `td.danimoya.com` login pass, valid TLS,
zero restarts. No production authenticated streaming workflow or load test was
performed. The legacy hostname TLS issue remains separately pending.
Initial checks ran before the listener was ready (connection refused/public502).
Rechecked the same running container without restarting: internal/public login200,
valid TLS, zero restarts and no recent Python exception types.

This limits admitted SSE streams only. It does not cap the overall HTTP request
thread pool, header-parsing connections, background jobs, other replicas, or all
pg8000 connections. Finding `e73515b89422` remains pending for that broader scope.

Private stopped-cache archive `/tmp/token-dashboard-sse-backup-20260921/cache.tar`
was catalog-checked, not restore-tested. No schema migration. Rollback uses the
nonroot image/base manifest and current data/secrets; it removes SSE protections
and should be reviewed accordingly. Rehearsal container removed.
