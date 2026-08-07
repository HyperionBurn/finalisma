# Weft bridge-adapter interop validation — 2026-08-05

## Summary

The **bridge adapters tier** (roadmap §3 #3) was validated end-to-end against a
**real spawned coordinator** (stdio transport). The bridge adapters are the path
for hosts that **cannot speak MCP** — the only way non-MCP AI products reach the
coordinator. This transcript proves the adapters work; it does **not** prove a
specific third-party host product (e.g. Slack webhook) integrates.

| Adapter | Happy path | Negative refusal |
| --- | --- | --- |
| ClipboardBridge | generate → parse accepted (nonce consumed) | 2nd parse refused: `bootstrap_reused` |
| PollingBridge | enqueue → get_pending → ack → no repeat | wrong token refused; non-member refused |
| WebhookBridge | register → deliver → HMAC verified | wrong secret rejected; no signing_secret refused (HIGH-1) |

## Honest scope statement

- **Proven:** the bridge adapter classes (`WebhookBridge`, `PollingBridge`,
  `ClipboardBridge`) in `src/weft_mcp/bridge.py` compose against a real
  coordinator's database and enforce their invariants (one-shot nonce,
  at-most-once cursor delivery, actor-bound access, HMAC-signed webhooks with
  fail-closed signing). All negative refusals fire as designed.
- **Not proven:** that any specific third-party non-MCP host product (Slack,
  Discord, a custom webhook consumer, a CLI-only agent) integrates. That requires
  a per-host integration test with that host's actual surface. This is the
  adapter *library* validation, not a host *integration* claim.
- **Not re-proven:** the MCP stdio protocol itself — that is covered by
  `docs/INTEROP_VALIDATION_2026-08-05.md`. Here the coordinator is the
  protocol engine and the bridge adapters are driven through it.

## Environment

- Host: Windows 11, Python 3.11+ (stdlib only — no third-party deps)
- Transport: **stdio** (MCP's primary transport)
- Coordinator: `scripts/weft-mcp.py --transport stdio --team-id demo`
- Driver: `scripts/interop-validate-bridge.py` (Python driver owns the child
  via `subprocess.Popen`, teardown in `finally`, run under `timeout`)

## Commands (exact)

```powershell
timeout 180 python -B scripts/interop-validate-bridge.py
python -B -m unittest tests.test_interop_bridge -v
netstat -ano | grep <webhook-port>   # must show NO listener after exit
python -B scripts/weft-smoke.py
```

## Result

Driver: `status: ok`, `time_s: 0.376`.

```
status: ok
protocol_version: 2025-11-25
server_info: {name: weft-mcp, version: 0.1.0}
tool_count: 58
clipboard_one_shot:
  first_parse_ok: true
  second_parse_refused: true
  refusal: "Bootstrap snippet has already been consumed"
polling_atmostonce:
  got_event: true
  no_repeat: true
  wrong_token_refused: true
  wrong_token_refusal: "Actor token mismatch"
  nonmember_refused: true
  nonmember_refusal: "No credential registered for this agent"
webhook_signed:
  delivered: true
  signature_verified_real_secret: true
  wrong_secret_refused: true
  no_signing_secret_refused: true
  no_signing_secret_refusal: "deliver requires the real webhook signing secret; the stored hash cannot be used as the signing key"
time_s: 0.376
```

Unittest: `Ran 7 tests in 1.432s — OK`.

Smoke: `evidence_passed: true`.

Netstat: NO LISTENER (clean) — the ephemeral webhook receiver was torn down.

## Verbatim transcript (driver)

```
> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{}}}
< {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"weft-mcp","version":"0.1.0"},...}}
# initialize: protocol=2025-11-25 server={'name': 'weft-mcp', 'version': '0.1.0'}
> {"jsonrpc":"2.0","id":2,"method":"tools/list"}
< {"jsonrpc":"2.0","id":2,"result":{"tools":[...58 tools...]}}
# tools/list: 58 tools
> {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"register_agent","arguments":{"team_id":"demo","agent_id":"bridge-agent","role":"generalist","name":"Bridge Agent"}}}
< {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{...\"actor_token\":\"fst_actor_w6e0nNc8iuTHLIMNUgCuhWkOvVOlr0g1TjpUXyor6mU\"...}"}],...}}
# register_agent: agent_id=bridge-agent token=fst_acto...
> {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"create_pairing","arguments":{"initiator_id":"bridge-agent","team_id":"demo","capabilities_offered":["read"],"actor_token":"fst_actor_w6e0nNc8iuTHLIMNUgCuhWkOvVOlr0g1TjpUXyor6mU"}}}
< {"jsonrpc":"2.0","id":4,"result":{...,"pairing_id":"pair_f1d90b2b038d4182948b81495c532787","join_token":"fst_pair_IkqJWyWwtm1d6zf3RYcAW7Utc7HqpLgIowamCuEnVTw",...}}
# clipboard.generate_bootstrap: pairing_id=pair_f1d90b2b038d4182948b81495c532787 nonce=d67ec2fe...
# clipboard.parse_bootstrap (1st): accepted, nonce consumed
# clipboard.parse_bootstrap (2nd) refused: Bootstrap snippet has already been consumed
# polling.enqueue: event_id=bo_e636d9c246490408be54ff9cea937957 seq=1
# polling.get_pending (cursor=0): returned 1 event(s), next_cursor=1
# polling.ack: acked_count=1 cursor=1
# polling.get_pending (cursor=1): returned 0 event(s) (at-most-once OK)
# polling.get_pending (wrong token) refused: Actor token mismatch
# polling.get_pending (non-member) refused: No credential registered for this agent
# webhook.register_webhook: webhook_id=wh_a6fe2c3998c41ba704ad91b0b7d8aa02 url=http://127.0.0.1:60489/hook
# webhook.deliver: delivered=True status=200
# webhook receiver: got_request=True signature=sha256=a5e8c60a38a4fb9e72ee7... timestamp=1785946409
# webhook.verify_signature (real secret): True
# webhook.verify_signature (wrong secret): False (correctly rejected)
# webhook.deliver (no signing_secret) refused: deliver requires the real webhook signing secret; the stored hash cannot be used as the signing key
```

## Negative refusals (verbatim)

| Case | Refusal |
| --- | --- |
| Clipboard 2nd parse | `Bootstrap snippet has already been consumed` (code: `bootstrap_reused`) |
| Polling wrong token | `Actor token mismatch` |
| Polling non-member | `No credential registered for this agent` |
| Webhook wrong secret | `verify_signature` returns `False` |
| Webhook no signing_secret | `deliver requires the real webhook signing secret; the stored hash cannot be used as the signing key` (code: `signing_secret_required`) |

## Status

**ok** — the bridge adapters tier is validated against a real coordinator with
all happy paths and all negative refusals passing. This satisfies roadmap §3
#3's requirement that a tier is only "supported" with a committed transcript.
