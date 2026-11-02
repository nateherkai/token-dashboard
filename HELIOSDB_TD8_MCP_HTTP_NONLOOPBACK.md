# TD#8 — Request: serve MCP-over-HTTP on a non-loopback bind (auth-safe)

**From:** the Token Dashboard integration (end-user / operator of an embedded HeliosDB-Nano).
**To:** the HeliosDB-Nano development agent.
**Against:** v3.37.0 (`82949fa`). **Severity (our view):** Medium — there is a workaround, but it's
fragile (see below). **Context:** this is the 8th item in the TD feedback series; **TD#1–#7 are all
fixed — thank you**, the MCP-over-HTTP + `CODE_EMBED` work in v3.37.0 is exactly what we asked for.
This request is the one gap that remains for a *containerized* deployment.

## The problem

v3.37.0 mounts the MCP-over-HTTP routes (`/mcp`, `/mcp/info`, `/mcp/ws`, `/mcp/sse`) on `--http-port`
**only when the daemon binds loopback** (`--listen 127.0.0.1`). With a non-loopback bind
(`--listen 0.0.0.0`) every `/mcp*` path returns **404** while `/health` still works. We understand
the intent ("non-loopback MCP listeners still require explicit auth-safe binding" — CHANGELOG) and we
agree it should not be exposed unauthenticated. The problem is there is **no flag to opt in**, so
non-loopback MCP is simply unreachable.

Repro:
```
# MCP works on loopback:
heliosdb-nano start --data-dir D --listen 127.0.0.1 --port 5440 --http-port 18090
curl http://127.0.0.1:18090/mcp/info        # => {"protocolVersion":"2024-11-05", ...}  ✅

# MCP is 404 on a non-loopback bind:
heliosdb-nano start --data-dir D --listen 0.0.0.0 --port 5440 --http-port 18090
curl http://127.0.0.1:18090/mcp/info        # => HTTP 404, body []                      ❌
curl http://127.0.0.1:18090/health          # => 200 {"status":"ok"} (health still served)
```
`start --help` exposes no `--mcp*`, `--http-listen`, `--allow-remote-mcp`, or env override.

## Why it matters (end-user impact)

Our dashboard runs in a **Docker container** and reaches HeliosDB over the **docker bridge gateway**
(`172.17.0.1`). For the container to reach the **PG-wire** at all, the daemon **must** bind
non-loopback (`0.0.0.0`). But on a non-loopback bind, MCP is 404 — so a containerized client can use
the database but **cannot use MCP-over-HTTP at all**.

The single `--listen` flag couples the PG-wire bind and the MCP-mount decision, and they pull in
opposite directions: PG-wire wants non-loopback (for the container); MCP wants loopback (to mount).

## Our current workaround (and why we want to drop it)

To get MCP, we now bind the daemon `--listen 127.0.0.1` and run **two `socat` bridges**
(`172.17.0.1:5440→127.0.0.1:5440` and `172.17.0.1:18090→127.0.0.1:18090`). This works, but:
- it makes **socat a single point of failure for ALL database access**, not just MCP;
- it **re-exposes** MCP on the bridge IP anyway — defeating the loopback gate, just less safely (no
  auth in front);
- it's extra moving parts to supervise (we put it under systemd).

We'd much rather use a first-class, auth-gated option.

## What we need (requirements)

Any **one** of these would let us drop the socat hack:

1. **`--http-listen <addr>` separate from `--listen`.** Let the health+MCP http-port bind
   independently of the PG-wire. We'd bind PG-wire `0.0.0.0` and http `127.0.0.1` (or vice-versa with
   auth). *Cleanest, most general.*
2. **`--mcp-listen <addr>` / `--allow-remote-mcp`.** Explicit opt-in to mount MCP on a non-loopback
   bind. Off by default (safe); on when the operator accepts responsibility.
3. **MCP honors `--auth`.** When `--auth password` (or a token) is set, mount MCP on the non-loopback
   http-port and require that credential on the MCP HTTP endpoint (e.g. `Authorization: Bearer <token>`
   or HTTP Basic). Then non-loopback is genuinely auth-safe.

## Expectations (acceptance criteria)

- With the chosen mechanism, `GET /mcp/info` and `POST /mcp` (initialize → `tools/list`) return real
  MCP responses when the http endpoint is reachable on a **non-loopback** address.
- **Default stays safe**: no mechanism set ⇒ current behavior (loopback-only) is unchanged.
- If the auth route is taken (option 3): unauthenticated requests to a non-loopback MCP endpoint get
  **401**, authenticated requests succeed. We can supply the same password/token our PG-wire client
  uses.
- Documented in `start --help` and the CHANGELOG, with the security note.
- A regression test that asserts MCP serves on a non-loopback bind under the opt-in/auth, and stays
  404 without it.

## Nice-to-have

- A short note in docs on the recommended pattern for **containerized** clients reaching MCP via the
  docker bridge — so others don't reinvent the socat workaround.

Repro environment: HeliosDB-Nano v3.37.0, features `code-graph,graph-rag,mcp-endpoint,code-embed`,
Token Dashboard in a Docker container on a user bridge network reaching the host via `172.17.0.1`.
