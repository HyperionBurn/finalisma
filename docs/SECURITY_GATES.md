# Weft production security gates

These gates turn model reviews into executable acceptance criteria. A model
report is a claim; a passing test and observed artifact are evidence. The
current SQLite coordinator is a single-node boundary; do not expose it to
untrusted public traffic without completing the multi-instance gates below.

## Local evidence map

The following map makes the boundary explicit. These tests exercise local
source and temporary stores. They do not prove that a hosted deployment runs
the same revision or configuration.

| Gate area | Local evidence | Hosted boundary |
|---|---|---|
| Pairing, consent, and idempotency | `tests/test_identity_invites.py`, `tests/test_identity_negatives.py`, `tests/test_room_cursor_guards.py`, `tests/test_room_receipts.py` | Re-run against an authorised release before making a hosted claim. |
| Origin, join limits, and rate limits | `tests/test_webapp_security.py`, `tests/test_auth_rate_limits.py`, `tests/test_invite_api_security.py` | A reachable endpoint is not proof that the deployed release enforces these checks. |
| Payload and workspace boundaries | `tests/test_input_hardening.py`, `tests/test_core_hardening.py` | No remote filesystem or payload-execution claim is made. |
| Tenant and actor isolation | `tests/test_tenancy.py`, `tests/test_tenancy_negative.py`, `tests/test_authz_plane.py` | Multi-instance storage and identity boundaries remain open work. |
| Hosted MCP and delivery contracts | `tests/test_hosted_mcp.py`, `tests/test_hosted_mcp_sse.py`, `tests/test_cloud_delivery_outbox.py` | These are local HTTP/service tests, not external provider or uptime proof. |
| Release and deployment wiring | `tests/test_vercel_config.py`, `tests/test_probe_live_release_contract.py`, `tests/test_site.py`, `tests/test_deploy_gate.py` | CI checks the contract. The read-only live probe must still pass for the exact origins. |
| Retention and operational scripts | `tests/test_deploy_ops.py`, `tests/test_deploy_backup.py` | Backup, restore, TLS, alerting, and runtime state need an operator observation. |

The multi-instance requirements below currently have no passing implementation
gate. Keep them marked as open until shared storage, distributed delivery,
remote identity validation, and failure-mode testing exist together.

## Must pass before trusted single-node pairing

- Pair tokens use `secrets.token_urlsafe`, are hashed before persistence, are
  one-use, expire, and cannot be replayed after a successful join.
- New identities receive an `actor_token` once. Only its SHA-256 hash is stored
  in `agent_credentials`; existing identities and team/work-plane calls require
  the bound token whenever actor authentication is enabled.
- A new invited `agent_id` can bootstrap its actor credential through pairing.
  An existing invitee must supply its current `actor_token`, so a pair token
  cannot overwrite or claim an existing identity.
- Session tokens are never returned by preview endpoints, never logged, and
  are stored only as hashes. They are member-bound session capabilities, not
  substitutes for actor credentials.
- `consent=true` is mandatory for join and must be a literal JSON boolean;
  strings and numbers are rejected without consuming the pairing. Capability
  policy is visible before it is accepted.
- Session events require a valid member-bound token, a unique idempotency key,
  and a monotonic sequence. Duplicate submissions return the original event.
- All public join attempts are size-limited, Origin-checked when an Origin is
  present, rate-limited with a `Retry-After` response, and reject token-bearing
  URL paths.
- The server never evaluates or executes task/message payloads.
- Workspace paths resolve beneath the configured root, including symlink
  resolution, before artifact hashing.
- Health endpoints reveal service health only, not tokens, paths, teams, or
  provider credentials.
- A remote single-workspace deployment can set `--team-id` so a bearer token
  cannot address another team through free-form request arguments.
- `--actor-auth auto` requires actor credentials for HTTP and trusts stdio.
  `--actor-auth trust` is loopback-HTTP-only, emits a warning, and is rejected
  on non-loopback hosts; `--actor-auth required` can also protect stdio.
- `rotate_agent_credential` replaces the actor token atomically and
  invalidates the old value. Schema-v2 migration creates schema v3 without
  inventing credentials for old identities; recovery requires trusted local
  rotation/bootstrap or migration to a newly paired identity.
- The `/mcp` endpoint is request-rate-limited and caps reserved concurrent
  requests per client; long-polling cannot consume an unbounded number of
  handler slots in the single-node process.
- Authenticated browser preflight is Origin-checked without requiring a bearer
  on the unauthenticated `OPTIONS` request.
- Retention cleanup is explicit and dry-run-first through
  `scripts/weft-prune.py`; it removes only terminal sessions, stale
  pairing credentials, and old audit events when an operator passes `--apply`.

## Must pass before multi-instance hosted traffic

The current SQLite coordinator is intentionally a single-node boundary. Do not
call it horizontally scalable until these are implemented:

- Shared transactional storage with tenant/owner filtering enforced at the
  data layer, not only at HTTP handlers.
- A distributed limiter and transactional outbox/dead-letter queue for event
  delivery and retry.
- OAuth/OIDC resource-server validation for remote MCP, including audience
  binding and key rotation; a single shared bearer token is only a local or
  trusted-network bootstrap.
- Structured audit events with trace IDs, redacted token fields, provider
  circuit breakers, bounded concurrency, and alerting.
- Load, partition, reconnect, replay, cross-tenant, stale-session, and
  actor/session token-rotation tests.

## Suggested adversarial tests

```text
pair token: join twice -> second attempt rejected
pair token: wait past expiry -> rejected without creating a session
actor token: use for another agent or team -> rejected
actor token: rotate concurrently -> one replacement wins; old token rejected
migration: open schema v2 -> schema v3 has no fabricated actor credentials
session token: use from a third agent -> rejected
session event: resend same idempotency_key -> one sequence only
session cursor: ack seq 1, then seq 0 -> cursor remains 1
HTTP: invalid Origin -> 403
HTTP: join burst -> 429 with Retry-After
payload: Python/eval/shell-looking text -> stored as data, never executed
tenant: alter team_id in a request -> no cross-team read or write
restart: reopen SQLite -> pair/session/event state remains coherent
```
