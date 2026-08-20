# Bridge Adapters for Non-MCP Hosts

Weft's core coordination speaks MCP. Many hosts — browser-based agents,
plain CLI tools, IDE plugins, mobile apps — cannot load an MCP server. The
bridge adapters make those hosts first-class participants using only HTTP and
JSON.

## Tier matrix

| Host type | Adapter | Direction | Requirements |
|---|---|---|---|
| Browser-based agent (ChatGPT-like, web UI) | `HttpBridgeClient` + `WebhookBridge` | Pull + push | Can make HTTP requests; can expose a webhook endpoint |
| Plain CLI agent (no MCP, no inbound network) | `PollingBridge` + `ClipboardBridge` | Pull only | Can make periodic HTTP requests; can read/write clipboard or stdin/stdout |
| IDE plugin (VS Code, Cursor, etc.) | `HttpBridgeClient` + `PollingBridge` | Pull + push | Can run background tasks; can poll or receive webhooks |
| Mobile app | `HttpBridgeClient` + `PollingBridge` | Pull + push | Can make HTTP requests; can poll in background |

## What each adapter does

### WebhookBridge

For hosts that **can receive** HTTP POST callbacks. The host registers a
per-agent webhook URL. Weft delivers signed envelope events to that URL.

- **Signature**: HMAC-SHA256 over `{timestamp}.{canonical_json_body}`, sent in
  the `X-Weft-Signature` header as `sha256=<digest>`.
- **Replay protection**: `X-Weft-Timestamp` header; receivers reject
  signatures older than `max_age_seconds` (default 300s).
- **Credential hygiene**: only the SHA-256 hash of the webhook secret is stored.
  The raw secret is never returned after registration.
- **SSRF boundary**: production delivery rejects loopback, private, link-local,
  metadata, and other special-use destinations, and never follows redirects.
  Local test/dev receivers require an explicit `allow_local_webhooks=True` opt-in.

### PollingBridge

For hosts that **cannot receive** push. A durable SQLite outbox holds events
per `(team_id, agent_id)`. The host polls `get_pending(cursor)` and acks
delivered events.

- **At-least-once until acknowledgement**: an unacked event can be returned
  again after a retry or reconnect. Acked events are not re-delivered.
- **Cursor checkpoint**: acknowledgements advance the per-agent cursor only
  through the highest contiguous acknowledged sequence, so an out-of-order ack
  never skips a lower unacknowledged event on reconnect.
- **Monotonic sequence**: events are numbered per `(team_id, agent_id)` in
  insertion order.

### ClipboardBridge

For the **CLI copy-paste tier**. Generates a one-shot bootstrap JSON snippet
containing the endpoint, team, pairing link, and consent contract. Any agent
can paste this snippet to bootstrap a session.

- **One-use enforcement**: each snippet carries a nonce; the nonce is consumed
  on first parse and rejected on reuse.
- **Strict URL validation**: rejects token-bearing URL paths (tokens belong in
  the `#fragment`, never the path).
- **Mirrors core pairing rules**: the snippet's `join_url` puts the token in
  the fragment (`/v1/join/<pairing_id>#token=...`).

### HttpBridgeClient

A tiny `http.client`-based wrapper that lets **any process** (no MCP library
required) perform the full "give a link" flow:

- `preview_link(url)` — non-mutating preview of a pairing link.
- `join_link(url, agent_id, consent=True)` — consume a pairing with explicit
  consent (type-strict: `consent` must be JSON `true`).
- `send_event(session_token, agent_id, kind, payload, idempotency_key)` —
  append to a session log.
- `poll_events(session_token, agent_id, after_seq)` — replay events after a
  cursor.
- `ack(session_token, agent_id, seq)` — acknowledge processed events.

URL fragment token handling matches core exactly: the token lives in the
`#fragment`, is sent in the POST body, and is never placed in the URL path.

## Actor authentication

Every bridge call is bound to `(team_id, agent_id)` plus an actor key proof:

- `register_webhook`, `enqueue`, `get_pending`, `ack`, `generate_bootstrap`
  all require a valid `actor_token` for the calling `(team_id, agent_id)`.
- The actor token is validated against the SHA-256 hash stored in
  `agent_credentials` — matching core's credential hygiene.
- A revoked or mismatched token raises `BridgeAuthError`.

## Integration walkthroughs

### Browser host (ChatGPT-like)

1. Register an agent via `HttpBridgeClient` calling the MCP
   `register_agent` tool.
2. Register a webhook URL via `WebhookBridge.register_webhook`.
3. On each envelope event, the browser host receives a signed POST, verifies
   the signature with `WebhookBridge.verify_signature`, and applies the event.

### Plain CLI agent

1. The operator runs a command that calls `ClipboardBridge.generate_bootstrap`
   to produce a pasteable JSON snippet.
2. The CLI agent pastes the snippet; the host calls
   `ClipboardBridge.parse_bootstrap` to validate and consume it.
3. The CLI agent polls for events with `PollingBridge.get_pending` and acks
   with `PollingBridge.ack`.

### IDE plugin

1. The plugin registers an agent on first use.
2. It uses `HttpBridgeClient` for the pairing flow (preview → consent → join).
3. For live updates it either polls (`PollingBridge`) or, if the IDE exposes
   a local webhook endpoint, registers a webhook.

### Mobile

1. Same pairing flow as the IDE plugin via `HttpBridgeClient`.
2. Background polling via `PollingBridge` when push is unavailable.

## Seams for later integration

The bridge module exposes clean entry points for wiring into the HTTP server:

- `init_bridge(store)` — idempotent schema creation; call once at startup.
- `WebhookBridge(store)`, `PollingBridge(store)`,
  `ClipboardBridge(store)` — callable classes bound to a `WeftStore`.

A future integration pass can expose these as HTTP routes (e.g.,
`POST /v1/bridge/webhook/register`, `GET /v1/bridge/poll`, etc.) without
modifying the bridge classes themselves.

## Truth boundaries

- These adapters require the coordinator to be reachable over HTTP. They do
  not create a new transport.
- A host still needs a way to make HTTP requests. The bridge does not install
  MCP into a product that lacks it.
- Webhook delivery is best-effort with a failure counter; critical consumers
  should also poll to recover from dropped deliveries.
