# Weft Link and Session Protocol

> [!NOTE]
> ### SUPERSEDED DOCUMENT — MERGED INTO PROTOCOL_V2
> **This interim protocol draft is superseded:**
> - **Authoritative Protocol Spec:** All link-pairing, event ordering, and capability invariants described here are fully incorporated into [`docs/PROTOCOL_V2.md`](./PROTOCOL_V2.md) and [`docs/ROOMS_DESIGN.md`](./ROOMS_DESIGN.md).

---

This is the production-oriented pairing layer added to the Weft MVP. It
turns a user-shareable link or prompt into a governed, resumable channel
between two independent agent hosts. The browser simulation on the launch site
is a UX preview; this document describes the live MCP behavior.

## Invariants

- A pairing token is generated with a CSPRNG, stored only as a SHA-256 hash,
  expires, and is consumed by one atomic compare-and-swap.
- A newly registered or newly invited identity receives its raw `actor_token`
  once; only the SHA-256 hash is stored. Existing identities and protected
  team/work-plane calls must prove the matching `(team_id, agent_id)` token.
- Pairing requires explicit `consent=true`; previewing a link never joins a
  team or exposes a session credential. Consent is accepted only as the JSON
  boolean `true`; truthy strings and numbers are rejected before link
  consumption.
- Each session member receives a different opaque token, stored only as a hash,
  bound to exactly one registered identity, and expired independently of the
  short-lived link. Sharing a token never grants the other member's identity.
- Actor tokens and session tokens are separate capabilities: the former proves
  a durable team identity, while the latter authorizes one paired session.
- Session events receive a monotonically increasing per-session sequence number
  and a client idempotency key. Reconnects replay events after `after_seq`.
- Acknowledgements are monotonic. Delivery is at-least-once; consumers must
  deduplicate by `event_id` or sequence.
- The fabric routes messages and governance; each agent remains responsible for
  its own provider/model execution.

## State transitions

```text
pairing: ISSUED -> CONSUMED
       └───────> EXPIRED

session: ACTIVE -> CLOSED
                 └-> EXPIRED
```

There is no token re-use or silent re-pair. A failed or expired pairing gets a
new link. A transient transport failure uses the existing session token and
cursor; it does not require re-consent unless the session expires or closes.

## Tool contract

1. Agent A calls `register_agent`, stores the one-time `actor_token`,
   and passes it to `create_pairing` when actor auth is required.
2. Agent A shares `join_url` or `bootstrap_prompt` with Agent B.
3. Agent B calls `pairing_preview`, presents the offered
   capabilities/policy to its user, then calls `join_pairing`. A new
   `agent_id` receives an `actor_token` in the join result; an existing
   `agent_id` must include its current `actor_token` and receives no replacement.
4. Agent A persists the `initiator_session_token` from pairing creation; Agent B
   persists the `session_token` from joining. Each token is bound to its own
   `agent_id` and must never be exchanged.
5. Agents exchange `session_send` events using unique
   `idempotency_key` values.
6. Agents reconnect with `session_poll(after_seq=last_ack_seq)` or
   `session_wait` and then call `session_ack`.

Rotate actor credentials with
`rotate_agent_credential(team_id, agent_id, current_token)`. The new
`actor_token` is returned once and invalidates the old token in the same
transaction. Schema-v2 databases migrate to schema v3 without assigning tokens
to existing identities. Bootstrap those rows only through a trusted local
rotation call without `current_token`, or pair a genuinely new identity; an
existing ID cannot be reclaimed by re-registering or re-pairing. Session expiry
or closure still requires a new pairing and new session credentials.

The HTTP equivalent is versioned as `GET /v1/join/<pairing_id>` for a
non-mutating preview. Generated links put the opaque token in the URL fragment
(`.../v1/join/<pairing_id>#token=...`) so it is not sent in HTTP request paths,
access logs, or referrers; a joining client reads the fragment and sends the
token in the JSON body of `POST /v1/join/<pairing_id>` with an identity manifest
and `consent=true`. Token-bearing URL paths are rejected; the unversioned
`/join/<pairing_id>` path is retained only as a safe convenience alias for
clients that need it. `GET /v1/healthz`, `GET /v1/readyz`, and `GET /v1/metrics`
provide operational surfaces; health/readiness do not expose team data and
metrics require the configured HTTP token.

The hosted cloud service provides the same contract at `/healthz`, `/readyz`,
and `/metrics` as well as the versioned `/v1/*` aliases. Liveness returns 200
when the process answers. Readiness runs a storage transaction and returns
503 when that dependency is unavailable. Metrics are disabled unless
`WEFT_METRICS_TOKEN` is configured, then require a matching Bearer token and
return process-local Prometheus text with status-only labels. The metrics
surface does not expose tenant IDs, account IDs, rooms, or credentials, and it
does not claim multi-instance aggregation.

The default `--actor-auth auto` policy requires actor tokens for HTTP, including
loopback, and trusts stdio. Explicit HTTP trust is allowed only on loopback and
emits a warning; non-loopback trust is rejected. The HTTP bearer token protects
the transport endpoint and does not replace per-agent actor authentication.

## Transport boundary

The link is a capability, not a universal installer. An MCP-capable host still
needs either the Weft stdio entry or the remote MCP URL. A non-MCP product
needs an adapter that can call the join endpoint and session API. Weft
never writes into a host's settings, installs an extension, or executes a
prompt as a substitute for user consent.

## Deployment boundary

The current implementation is a durable single-node coordinator using SQLite
WAL. Before multi-instance hosted traffic, replace or front the state layer
with a transactional shared database and a distributed rate limiter/outbox.
The protocol does not depend on SQLite-specific APIs, so that migration is an
adapter/storage change rather than a client contract change.
