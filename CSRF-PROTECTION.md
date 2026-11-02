# CSRF protection and API migration

With cookie authentication enabled, every JSON POST requires `X-TD-CSRF`.
Authenticate normally, then GET `/api/csrf` with the session cookie to obtain
`csrf_token`. Send that value in `X-TD-CSRF` along with the session cookie.
Bootstrap responses are non-cacheable. Do not log tokens or session cookies.

Tokens are bound to a verified session, not merely to the username. New session
cookies include a random nonce. Existing two-field session cookies remain valid
and receive a domain-separated HMAC-derived CSRF token, so deployment does not
force existing users to sign in again. The signing secret is unchanged.

Login uses a separate short-lived, signed, double-submit cookie and hidden form
token. Cookie values remain HttpOnly/Secure. Missing, expired or mismatched
tokens fail before credential checks or mutation handlers. SameSite remains
defense in depth, not the sole CSRF control.

These former mutation GET endpoints now require POST:

- `/api/scan`
- `/api/scan/force`
- `/api/savings/backfill`
- `/api/helios/refresh-mvs`
- `/api/helios/bulk-migrate` (send `limit` in the JSON body)
- `/logout`

GET returns 405 and performs no mutation. External integrations using the old
GET contract must migrate. Repository browser callers were updated; scoped
searches of local scripts/cron configuration found no additional matching
consumers. This is not proof that no external callers exist.

The shared browser API helper retrieves and caches the token and retries
bootstrap after transient failures. Authentication-disabled open mode remains
intentionally unprotected; use it only in the original trusted local context.
Production cookie authentication was confirmed enabled before rollout.

Release: `token-dashboard:csrf-20260921`, also tagged `:latest`. The release
overlays only reviewed auth/server/browser changes on the prior validated image;
unrelated dirty budget/Helios changes remain excluded. Rollback container:
`token-dashboard-pre-csrf-20260921`, stopped and disconnected from the network.

Validation: 80 tests pass in the isolated release image, plus synthetic browser
helper checks. Existing non-failing test socket ResourceWarnings remain.
Production checks use the public login page and runtime artifact hashes; no
production login submissions or state-changing requests are used.
