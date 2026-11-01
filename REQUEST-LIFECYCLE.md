# Bounded request and connection lifecycle — 2026-09-21

Deployment superseded by `LOGIN-LIMITS.md` and `compose.login.yml`; all lifecycle
controls described here remain in the current image.

Deployed `token-dashboard:lifecycle-20260921`, image ID
`sha256:d555add68452e58b60f5342c4429ea0586a0923aedc27aff016c1ea419cdc067`.
Use `/home/app/token-dashboard-live/compose.yml` plus `compose.lifecycle.yml`.
The reviewed server/writer artifacts and two small lifecycle modules layer onto
the SSE release. Pending budget source changes remain excluded. Nonroot runtime,
environment, command, mounts, dependencies and database engine are unchanged.

## Controls

- At most64 HTTP worker threads per server process. Admission is nonblocking
  before thread creation; excess sockets close immediately rather than spawning
  more workers or building an unbounded queue. Workers are daemon threads.
- Accepted sockets have a30-second idle I/O timeout. SSE retains its tighter
  write deadline,300-second lifetime and separate32/global,4/session budget.
  Socket timeouts are not a wall-clock execution deadline for arbitrary work.
- The owning HTTP worker closes its cached pg8000 connection when it exits.
  Slots release even on handler/cleanup/thread-creation errors. Cleanup does not
  reset the process's already-checked schema flag or perform schema migration.
- One scan runs at a time across periodic, manual and forced scans. Busy manual
  requests return409. Forced scans reserve the slot before creating a thread or
  clearing file offsets. Each completed/failed scan closes its thread-local
  connection; the periodic sleeper therefore does not retain an idle connection.

These are per-process service limits, not a database-wide quota across other
apps, CLI processes, replicas or feature-specific transient raw connections.
They do not replace authentication, rate limiting or slow-query remediation.

## Validation

38 focused tests and71 existing immutable-image tests pass. Coverage includes
worker admission, real socketpair rejection/closure, timeouts, constructor/start
failure, cleanup in the worker thread, scan singleflight and rollback of busy
admission, cleanup errors, actual server wiring, and existing SSE/auth behavior.
The parent caught a wrong close_request signature masked by initial mocks and
required a real socket closure test before accepting the lower-cost agent's code.
Pre-existing test socket ResourceWarnings remain; no production load test ran.

The actual bounded server starts in isolation and serves login as UID1001.
All four deployed artifacts match the tested image. After readiness polling,
production internal/public td.danimoya.com login returns200 with valid TLS and
zero restarts. A read-only SELECT1 using the existing configured password passes;
the actual cleanup helper closes that driver connection, clears its thread-local
reference, and reuse is rejected with InterfaceError. No schema initializer,
production login submission or synthetic production scan was invoked for tests.
The legacy hostname TLS issue is unchanged.

Private stopped-cache archive is under
`/tmp/token-dashboard-lifecycle-backup-20260921`; catalog-checked, not restore-
tested. No schema migration. Rollback uses the SSE release and current data/
secrets, but removes the HTTP/scan lifecycle controls and requires review.
Rehearsal container removed; no active sub-agent or test process remains.
