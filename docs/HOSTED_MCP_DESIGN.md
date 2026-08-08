# Hosted MCP endpoint — authenticated, tenant-confined

**Status:** implemented (`src/weft_cloud/mcp.py`), endpoint at `POST /mcp` on
the hosted `weft-cloud` service, 10 integration tests
(`tests/test_hosted_mcp.py`) driving the real HTTP surface.

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
| Cloud (`weft_cloud`) | `fss_` session token → `SessionContext{tenant, account, role}` | Real accounts, structural tenancy |
| MCP (`weft_mcp`) | `team_id` + `fst_actor_` actor token | Open `register_agent`, caller-picked team |

The hosted MCP endpoint authenticates **exclusively against the cloud plane**:
every request must present a valid `fss_` session token. The token resolves to
a tenant and an account, and every tool call is confined to that tenant by
routing through `CloudRoomService` — the same service `/v1` drives. No valid
credential → refused.

Why session tokens and not hosted actor tokens: an actor token in the
self-hosted model is caller-minted and team-scoped, with no tenant binding.
Binding `fst_actor_` tokens to tenants would mean inventing a new credential
type and a new minting authority — a larger change with a new attack surface.
Reusing the existing session token as the actor credential (the same
convention `/v1/rooms/create` and `/v1/rooms/join` already use) makes a
member identity created over MCP indistinguishable from one created over REST,
and gives every call a tenant for free.

### Identity is never an argument

The MCP tool schemas deliberately have no `team_id`, `agent_id`, or
`actor_token` inputs, and `additionalProperties: False` plus an explicit
argument check reject them if supplied. `agent_id` is always the authenticated
`account_id`; the actor credential is always the session token. This is
stricter than `/v1`, which accepts `owner_agent_id`/`agent_id` from the body
and defaults them to the session account — a within-tenant spoofing gap this
surface does not inherit.

## Tools exposed (and withheld)

Exposed — the room set the product promise depends on, one MCP tool per
`CloudRoomService` method (tenant = session tenant, agent = session account):

`room_create` `room_join` `room_send` `room_poll` `room_info` `room_ack`
`room_heartbeat` `room_event_log`

Withheld — the other ~50 self-hosted tools (`register_agent`, pairing, task,
roster, outbox, bridge, metrics, tenancy, …). They assume the self-hosted
team/actor-token model and each would need a per-tenant reimplementation to be
safe to serve. A smaller correct surface beats a large unsafe one; the room
tools are the surface the product's "one link, many agents" promise depends
on. The tool set is pinned by a test
(`test_hosted_surface_is_a_small_correct_set`).

## Tenant confinement and the no-oracle property

Every tool resolves the effective tenant the same way the `/v1` handlers do.
`room_create` and `room_join` run in the caller's session tenant; the
member-gated tools (`room_info`, `room_poll`, `room_ack`, `room_heartbeat`,
`room_send`, `room_event_log`) resolve the tenant via the caller's membership
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
  `room_heartbeat`, `room_send`, `room_event_log`, and `room_join`.
- `room_join` is the only cross-tenant path, and it is gated by a secret link:
  `_resolve_room_for_link` looks up `(room_id, token_hash)` and returns
  `invalid_link` whether the room is foreign or nonexistent. Knowing a room_id
  is not a capability. Holding a valid link grants membership (and then
  membership, not the link, is the ongoing capability —
  `test_cross_tenant_link_join_grants_membership_not_privilege` proves the
  joiner operates, and a third tenant with no link stays `room_not_found`).
- Auth failures are byte-identical whether the token is missing, malformed,
  unknown, or revoked (`test_unauthenticated_calls_refused_identically`) — the
  endpoint is not an oracle for session validity either.

Within a caller's own tenant, a non-member gets `member_required` — the same
semantics `/v1` already has, and room_ids are 128-bit random secrets, so this
adds no enumeration surface relative to the existing API.

## Same room, both surfaces

There is exactly one room store: `CloudRoomService` over the cloud SQLite-WAL
database. The hosted MCP tools call the identical methods the `/v1` handlers
call, with the same tenant binding and the same plan-quota enforcement
(room member cap, enforced atomically inside `CloudRoomService.join_room`).
`test_room_created_via_mcp_is_the_same_room_as_v1` proves it in both
directions: create over `/v1`, join + message over MCP; create over MCP,
join + message over `/v1`. `test_plan_room_member_cap_enforced_through_hosted_mcp`
shows the cap refusal (`room_full`) through the MCP path.

## MCP protocol details

- `POST /mcp`, Streamable-HTTP JSON-RPC framing identical to the coordinator:
  `initialize` negotiates the protocol version, `tools/list` returns the 8
  tools, `tools/call` returns `structuredContent` (or an `isError` result with
  a structured `{"error": {code, message}}`), notifications
  (`notifications/initialized`, `notifications/cancelled`) return 202 with no
  body, `ping` → `{}`, `resources/list`/`prompts/list` → empty.
- `Mcp-Method` / `Mcp-Name` header validation is mirrored from the coordinator.
- `GET /mcp` → 405 (streaming GET is not enabled).
- Auth is checked before any method dispatches, so an unauthenticated
  `initialize` / `tools/list` / `tools/call` are all refused with the same
  generic 401 JSON-RPC error.

## Deployment

The endpoint lives **inside the existing `weft-cloud` process** (port 18788).
No new systemd unit and no new required env vars — auth is the existing cloud
session. Only nginx needs a route:

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
`https://<origin>/mcp` and must send `Authorization: Bearer <fss_ session>`
with every request. No URL is hardcoded anywhere; the service binds loopback
and nginx owns the public origin, following the existing
`WEFT_PUBLIC_URL` / env-driven-origin pattern.

Systemd: reuse the existing `weft-cloud` unit; there is nothing new to run.
Config is unchanged: `WEFT_HOST=127.0.0.1`, `WEFT_PORT=18788`,
`WEFT_DB_PATH=/var/lib/weft/cloud.db`.

## Scope decisions made deliberately

1. **Session-token-only auth.** No new actor-token type; see the security
   decision above.
2. **Room tools only.** The remaining ~50 self-hosted tools are withheld until
   each has a tenant-confined reimplementation; a negative claim is recorded in
   the report rather than shipped as unsafe surface.
3. **Plan-level `max_rooms` / `max_events` / `max_messages` are NOT wired into
   this path** — because they are not wired into `/v1` or the web app either
   (the `quotas.py` helpers are unit-tested but orphaned). The hosted MCP path
   enforces exactly the same plan limits the REST surface enforces (the room
   member cap). Wiring the plan helpers into the room lifecycle is a separate
   product decision affecting all surfaces; it is noted as a follow-up rather
   than done inconsistently on one surface only.
