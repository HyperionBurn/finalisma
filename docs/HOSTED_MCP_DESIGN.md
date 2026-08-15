# Hosted MCP endpoint — authenticated, tenant-confined

**Status:** implemented (`src/weft_cloud/mcp.py`), endpoint at `POST /mcp` on
the hosted `weft-cloud` service, 27 integration tests
(`tests/test_hosted_mcp.py`) driving the real HTTP surface, plus 8 SSE
framing tests (`tests/test_hosted_mcp_sse.py`) and 1 agent-key auth test
(`tests/test_hosted_mcp_agent_keys.py`).

## The gap this closes

The self-hosted coordinator (`weft_mcp`) speaks MCP over `POST /mcp` on a
loopback port and opens with `register_agent` — anyone who can reach it can
register into any `team_id` and read that team's rooms. That is fine on a
developer's own machine and fatal on the public internet.

The hosted service (`weft_cloud`, port 18788) already had the identity plane
(accounts, sessions, tenants) and the room plane (`CloudRoomService`, plan
quotas) behind `/v1`. What it lacked was an MCP surface. This module mounts
the MCP protocol on that existing hosted service so an MCP client can connect
over HTTPS without running a coordinator locally.

## Security decision

Two identity systems exist in this repo and they are NOT interchangeable:

| Plane | Credential | Model |
|---|---|---|
| Cloud (`weft_cloud`) | `fss_` session or `agk_` agent key → `SessionContext{tenant, account, role, agent_id}` | Real accounts, structural tenancy; an agent key carries its own room identity |
| MCP (`weft_mcp`) | `team_id` + `fst_actor_` actor token | Open `register_agent`, caller-picked team |

The hosted MCP endpoint authenticates **exclusively against the cloud plane**:
every request must present a valid `fss_` session or `agk_` agent key. The
credential resolves to a tenant, an account, and an agent identity, and every
tool call is confined
to that tenant by routing through `CloudRoomService` — the same service `/v1`
drives. Agent-key creation, listing, and revocation remain session-only. No
valid credential → refused.

Why cloud credentials and not hosted actor tokens: an actor token in the
self-hosted model is caller-minted and team-scoped, with no tenant binding.
Binding `fst_actor_` tokens to tenants would mean inventing a new credential
type and a new minting authority — a larger change with a new attack surface.
Reusing the existing cloud credential as the actor credential (the same
convention `/v1/rooms/create` and `/v1/rooms/join` already use) makes a
member identity created over MCP indistinguishable from one created over REST,
and gives every call a tenant for free.

### Identity is never an argument

The MCP tool schemas deliberately have no `team_id`, `agent_id`, or
`actor_token` inputs, and `additionalProperties: False` plus an explicit
argument check reject them if supplied. `agent_id` is always the authenticated
identity — the `account_id` for a session, a key-derived identity for an agent
key, so one account running several keys gets several distinct room members —
never a client-supplied value; the actor credential is the bearer session or agent key. This is
stricter than `/v1`, which accepts `owner_agent_id`/`agent_id` from the body
and defaults them to the authenticated identity — a within-tenant spoofing gap this
surface does not inherit.

## Tools exposed (and withheld)

Exposed — the room set the product promise depends on, one MCP tool per
`CloudRoomService` method (tenant = authenticated cloud tenant, agent = authenticated identity):

`room_create` `room_join` `room_send` `room_receipts` `room_poll` `room_wait`
`room_info` `room_ack` `room_heartbeat` `room_leave` `room_remove_member`
`room_event_log`

The member lifecycle is complete: `room_leave` frees a member's seat
immediately (`room.left` event, history and attribution preserved), and the
owner-only `room_remove_member` frees a target member's seat — removal is NOT
a ban (a removed member who still holds a valid link can rejoin). The tool set
is pinned by a test (`test_hosted_surface_is_a_small_correct_set`), which
asserts exactly 12 tools.

Withheld — the other ~46 self-hosted tools (`register_agent`, pairing, task,
roster, outbox, bridge, metrics, tenancy, …). They assume the self-hosted
team/actor-token model and each would need a per-tenant reimplementation to be
safe to serve. A smaller correct surface beats a large unsafe one; the room
tools are the surface the product's "one link, many agents" promise depends
on.

## Tenant confinement and the no-oracle property

Every tool resolves the effective tenant the same way the `/v1` handlers do.
`room_create` and `room_join` run in the caller's authenticated cloud tenant; the
member-gated tools (`room_info`, `room_poll`, `room_wait`, `room_ack`,
`room_heartbeat`, `room_send`, `room_receipts`, `room_event_log`,
`room_leave`, `room_remove_member`) resolve the tenant via the caller's membership
row (`CloudRoomService._resolve_room_tenant`, exactly as
`WeftCloudService._room_tenant` does), so a member who joined through a link
in another tenant can operate in the room's tenant. Because resolution goes
through *membership*, a caller can only ever resolve a room they are an active
member of. Consequences:

- Tenant B calling any member-gated tool on tenant A's `room_id` hits
  `_require_room(tenant_B, room_id_A)` → `room_not_found` — **byte-identical**
  to a room that does not exist. Proven by
  `test_cross_tenant_isolation_room_id_alone_yields_no_oracle`, which asserts
  the two error bodies are equal for `room_info`, `room_poll`, `room_ack`,
  `room_heartbeat`, `room_send`, `room_receipts`, `room_event_log`, and `room_join`.
- `room_join` is the only cross-tenant path, and it is gated by a secret link:
  `_resolve_room_for_link` looks up `(room_id, token_hash)` and returns
  `invalid_link` whether the room is foreign or nonexistent. Knowing a room_id
  is not a capability. Holding a valid link grants membership (and then
  membership, not the link, is the ongoing capability —
  `test_cross_tenant_link_join_grants_membership_not_privilege` proves the
  joiner operates, and a third tenant with no link stays `room_not_found`).
- Auth failures are byte-identical whether the token is missing, malformed,
  unknown, or revoked (`test_unauthenticated_calls_refused_identically`) — the
  endpoint is not an oracle for credential validity either.

Within a caller's own tenant, a non-member gets `member_required` — the same
semantics `/v1` already has, and room_ids are 128-bit random secrets, so this
adds no enumeration surface relative to the existing API.

## Same room, both surfaces

There is exactly one room store: `CloudRoomService` over the cloud SQLite-WAL
database. The hosted MCP tools call the identical methods the `/v1` handlers
call, with the same tenant binding and the same plan-quota enforcement
(room count at create, member cap enforced atomically inside
`CloudRoomService.join_room`, and the per-minute message + monthly event
budgets at send).
`test_room_created_via_mcp_is_the_same_room_as_v1` proves it in both
directions: create over `/v1`, join + message over MCP; create over MCP,
join + message over `/v1`. `test_plan_room_member_cap_enforced_through_hosted_mcp`
shows the cap refusal (`room_full`) through the MCP path.

**REST parity for receipts and removal.** Every room tool has a `/v1` twin;
the two newest are `POST /v1/rooms/receipts` (sender-scoped delivery/read
state, `service.py::handle_room_receipts`) and
`POST /v1/rooms/remove_member` (owner-only seat release,
`service.py::handle_room_remove_member`). Both call the same
`CloudRoomService.receipts` / `CloudRoomService.remove_member` methods the MCP
tools call, with the same tenant resolution via the caller's membership and
the same refusal codes — nothing on one surface is weaker than the other.

## `room_wait` — the blocking long-poll

`room_poll` returns immediately, so an agent polls once, has nothing left to
do, and its turn ends. `room_wait` is the primitive that keeps an agent
inside its turn: it blocks until at least one event with `seq > after_seq` is
available, then returns — the caller loops `wait, react, wait again` with no
human in the loop. Semantics (ordering, redaction, `next_seq`/cursor
reporting) are identical to `room_poll`, because `room_wait` is implemented
as a loop over the very same `CloudRoomService.poll` read; a blocking read can
never become a way around confidentiality.

- **Arguments** mirror `room_poll` (`room_id`, `after_seq`, `limit`) plus
  `timeout_seconds` (default 20, clamped to a 30 max) and the optional
  `message_kinds` filter — so it is a drop-in for `room_poll`.
- **Timeout is a normal outcome, not an error.** The result is an empty
  `events` list with `timed_out: true`; the caller just waits again.
- **Lock discipline.** Each internal poll is a fresh short read that opens
  and closes its own transaction before the sleep, so no SQLite write lock or
  transaction is ever held while a waiter sleeps. A blocked waiter does not
  freeze writers in any room (`test_blocked_waiter_does_not_block_writers`).
  `after_seq` is pinned on the first read, so a concurrent ACK cannot move
  the read window mid-wait.
- **Concurrency.** Many simultaneous waiters is the normal case. Each wait is
  bounded in duration (≤30 s) and holds one worker thread. A global
  fail-fast cap (`_WAIT_MAX_CONCURRENT = 128`) refuses new waiters when every
  slot is parked rather than stacking unbounded threads; a refused caller
  falls back to `room_poll`.
- **Rate limiting.** `room_wait` is a read and consumes nothing from the
  per-minute message budget (`max_messages_per_minute`, enforced at
  `room_send` time). It cannot be used to evade that limiter because it has no
  write path; the internal poll loop is one logical call, not N rapid polls.
- Tests: `test_hosted_mcp.py::HostedMCPRoomWaitTests` (immediate return,
  wake-on-event latency, empty-at-timeout, redacted-envelope confidentiality,
  identity-argument rejection, 4-concurrent-waiters ordering agreement, and
  blocked-waiter-does-not-block-writers).

## Cursor validation (`room_poll` / `room_wait` / `room_ack`)

Silence must never mean success for reads OR acks. The cursor arguments are
validated inside `CloudRoomService` (`src/weft_cloud/rooms.py`), so the
behaviour is byte-identical on both surfaces — the MCP tools and the `/v1`
REST handlers call the same methods:

- `after_seq` / `limit` that are not integers (including booleans) →
  `invalid_argument` (HTTP 400). A malformed cursor previously escaped as a
  `ValueError` → 500.
- `after_seq < 0` → `invalid_cursor` (HTTP 400) — never silently clamped.
- `after_seq > cursor_head + 1` → `invalid_cursor` — an impossible read window
  is refused, never silently echoed back as `next_seq`.
- `room_ack` `seq` not an integer → `invalid_argument`; `seq < 0` →
  `invalid_cursor`; `seq > cursor_head` → `invalid_cursor` (an ack of an event
  that does not exist is the caller's error).
- `room_wait` inherits the same guards through its internal `poll` loop;
  `timeout_seconds` is coerced and clamped (0..30) rather than refused.

The SDK maps `invalid_cursor` to `ConflictError` (`src/weft_sdk/client.py`
`_ERROR_MAP`).

## MCP protocol details

- `POST /mcp`, Streamable-HTTP JSON-RPC framing identical to the coordinator:
  `initialize` negotiates the protocol version, `tools/list` returns the 12
  tools, `tools/call` returns `structuredContent` (or an `isError` result with
  a structured `{"error": {code, message}}`), notifications
  (`notifications/initialized`, `notifications/cancelled`) return 202 with no
  body, `ping` → `{}`, `resources/list`/`prompts/list` → empty.
- `Mcp-Method` / `Mcp-Name` header validation is mirrored from the coordinator.
- `GET /mcp` → 405 (streaming GET is not enabled).
- Auth is checked before any method dispatches, so an unauthenticated
  `initialize` / `tools/list` / `tools/call` are all refused with the same
  generic 401 JSON-RPC error.

### Transport: Accept negotiation and SSE streaming

The request's `Accept` header is honoured (Streamable HTTP):

- `Accept: text/event-stream` **only** → the response is a `text/event-stream`
  stream. The JSON-RPC payload is framed as `data: <json>\n\n`; while a
  blocking call (`room_wait`) holds the connection open, `: keepalive\n\n`
  comment frames are emitted every **12 seconds** — deliberately below the
  20–30 s idle floors of common proxies, load balancers and client read
  timeouts (so a stream is never dropped as idle), yet far above the wait
  loop's 0.25 s poll cadence (so it is ~5 bytes per 12 s of traffic). A 20 s
  (default) wait emits one keepalive before the final frame; a 30 s (max)
  wait emits two. The keepalive interval is a handler class attribute
  (`_CloudHTTPHandler.mcp_sse_keepalive_seconds`) so it can be tuned or
  shrunk in tests.
- `Accept: application/json` (alone **or alongside** `text/event-stream`) →
  the long-standing plain-JSON response, byte-for-byte as before. Choosing
  JSON whenever the client accepts it keeps every currently-working caller
  unchanged; SSE is used only when the client explicitly excludes JSON.
- `Accept` that matches neither → **406** with a JSON-RPC error body.
- Auth outranks negotiation: a failed authentication is always the same
  byte-identical JSON 401 whatever media type the client asked for, and the
  endpoint never starts an SSE stream for a request that cannot be
  authenticated.
- Notifications (`id` absent) keep their 202 empty response under every
  Accept value.

The response body of an SSE response has no `Content-Length`; it is delimited
by connection close after the final `data:` frame, so the client sees EOF
exactly when the stream ends. The stdio bridge
(`src/weft_mcp/stdio_bridge.py`) already unwraps both shapes (plain JSON and
`event:`/`data:`-framed SSE), and the coordinator-side client code has always
parsed `data:` prefixes; this change adds the server side only.

- Tests: `test_hosted_mcp_sse.py` (SSE framing with JSON-path payload parity,
  JSON-only unchanged, both→JSON, neither→406, byte-identical 401s across
  negotiated types, notification 202 under SSE, keepalive-before-final-frame
  for a long `room_wait`).

## Deployment

The endpoint lives **inside the existing `weft-cloud` process** (port 18788).
No new systemd unit and no new required env vars — auth is the existing cloud
credential (`fss_` session or `agk_` agent key). Only nginx needs a route:

```nginx
# /etc/nginx/conf.d/weft.conf (fragment — add before the `location /` web-app block)
location = /mcp {
    proxy_pass http://127.0.0.1:18788;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
}
```

`location = /mcp` is an exact match, so it wins over the `location /` web-app
block and over `location /v1/` (which is prefix-scoped). MCP hosts connect to
`https://<origin>/mcp` and must send `Authorization: Bearer <fss_ session>` or
`Authorization: Bearer <agk_ agent key>`
with every request. No URL is hardcoded anywhere; the service binds loopback
and nginx owns the public origin, following the existing
`WEFT_PUBLIC_URL` / env-driven-origin pattern.

Systemd: reuse the existing `weft-cloud` unit; there is nothing new to run.
Config is unchanged: `WEFT_HOST=127.0.0.1`, `WEFT_PORT=18788`,
`WEFT_DB_PATH=/var/lib/weft/cloud.db`.

## Scope decisions made deliberately

1. **Cloud-credential-only auth.** No new actor-token type; both `fss_`
   sessions and `agk_` agent keys use the existing cloud identity funnel.
2. **Room tools only.** The remaining ~46 self-hosted tools are withheld until
   each has a tenant-confined reimplementation; a negative claim is recorded in
   the report rather than shipped as unsafe surface.
3. **Plan-level limits are enforced by `CloudRoomService` — the same code the
   `/v1` REST surface and the web app drive.** `max_rooms` is checked at
   `room_create` (`bind_room_with_quota_in_tx`), `max_members_per_room` at
   create and join (`validate_room_cap` / `increment_room_member_counter`),
   and `max_messages_per_minute` plus `max_events_per_month` at `room_send`
   (`RateLimiter().enforce` / `enforce_events_per_month`). Refusals carry the
   caller's own plan and limit (`quota_exceeded`, `rate_limited` +
   `Retry-After`) with the same code + message shape as `/v1`. Nothing on this
   surface is weaker than REST, and no limit is enforced inconsistently on one
   surface only.
