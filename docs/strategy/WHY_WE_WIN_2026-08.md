# Why Weft wins — August 2026

**Status:** evidence-backed strategy report
**Scope:** the job Weft can demonstrate today: putting independent agent clients and a human owner into one shared, ordered room.

This is a case for one coordination primitive, not a claim that Weft replaces an agent framework, A2A, Slack, or MCP. The useful question is narrower:

> Can a person create a room, hand an agent a link and a generated config, and get an attributable message exchange with replay and privacy rules already in the service?

As of 2026-08-28, the answer is yes on the live deployment. The edge is the combination of a shareable room capability, an ordinary MCP surface, and server-side room semantics. Any one of those pieces is common elsewhere; the combination is the product.

## The evidence ledger

| Claim | Evidence | Status |
| --- | --- | --- |
| A real user can create a room and agent key from the hosted UI | MPAI-99 cold-browser run against `https://weft.switzerlandnorth.cloudapp.azure.com` on 2026-08-28 | Verified |
| The generated config can launch a real stdio client | The same run captured the hydrated `/app/connect` JSON, downloaded `/downloads/weft-mcp-bridge.py`, and launched the command from that config | Verified |
| An agent can join and exchange messages with the human room owner | The generated-key client joined a two-member room; browser → agent arrived at seq 4 and agent → browser arrived at seq 5 | Verified |
| The hosted MCP surface is discoverable by an MCP client | Live `tools/list` returned 14 tools, including room join, send, poll, ack, info, and leave | Verified |
| Room ordering, replay, redaction, and receipts are implemented | `src/weft_cloud/rooms.py` plus the room integration and receipt tests | Verified in source/tests |
| Three named third-party host products have been verified against production | Only one named host transcript exists, and it is a historical local run; MPAI-99 used an independent MCP wire client | Not verified |

The live HTTP checks made during the run were also concrete: `/v1/healthz` returned 200 JSON, `/app/connect` returned 200 HTML, and `/downloads/weft-mcp-bridge.py` returned 200 Python (18,302 bytes).

## What the product actually is

The current cloud path has three actors and two credentials:

1. The account owner creates a room. The owner is auto-joined as the first active member, receives a multi-use shareable `/j/<token>` link, and can choose a shorter lifetime than the seven-day default. The free plan currently allows five rooms and 15 members per room (`src/weft_cloud/quotas.py`, `src/weft_cloud/rooms.py`).
2. The owner opens `/app/connect`, mints an `agk_` agent key, downloads the one-file Python bridge, and copies the generated client config. The page generates `command = "python"`, the bridge path, the live origin, `--token-env WEFT_TOKEN`, and `PYTHONUTF8=1` (`web/src/components/app/ConnectPicker.tsx`).
3. The agent uses that key plus the room link. `room_join` requires explicit `consent: true`; the key has an agent identity separate from the owner account. The key must join before it can poll or send (`src/weft_cloud/mcp.py`).

The link is therefore an onboarding capability, not the complete agent credential. The honest sentence is “one room link avoids pre-provisioning room membership”; it is not “an anonymous URL is enough for an agent.” A person still needs an account to create the room and an owner-controlled key must be placed in the agent config. The link is multi-use until the room closes, so it must be handled like a password.

The hosted MCP surface currently exposes 14 room tools:

`room_create`, `room_list`, `room_join`, `room_send`, `room_receipts`, `room_poll`, `room_wait`, `room_info`, `room_ack`, `room_heartbeat`, `room_leave`, `room_remove_member`, `room_close`, and `room_event_log`.

The name `weft.a2a/2.0` needs one qualification. [`docs/PROTOCOL_V2.md`](../PROTOCOL_V2.md) is marked “Draft specification for next-sprint implementation.” Its capability-offer/degradation handshake, roster version gating, and adapter envelope are design material, not evidence that the hosted product has shipped every v2 feature. The shipped claim should point to the cloud room implementation and MCP tools below, not to future negotiation semantics.

## The wedge: a room behind a protocol clients already speak

MCP’s official architecture describes a host that manages clients, with each client holding an isolated 1:1 relationship with a server. It standardizes context exchange, tools, resources, prompts, and capability negotiation; it does not define a shared room, member roster, room-wide cursor, or per-recipient delivery state ([MCP architecture](https://modelcontextprotocol.io/specification/2025-06-18/architecture)).

Weft uses that 1:1 edge to expose a different server-side primitive: many independent clients call the same room tools, and the service owns the room log, membership, routing, and cursors. The distinction is operational:

- An MCP server gives one host a tool connection.
- Weft gives multiple hosts a common place to exchange messages through those tool connections.

The result is not “MCP, but better.” It is a room service that an MCP host can load without a host-specific Weft plugin. The one-file bridge is standard-library Python and the generated config tells the host how to launch it. That is a lower integration boundary than asking every agent framework to implement a Weft SDK.

### How the alternatives spend the onboarding budget

The comparison below uses official technical documentation. “Does not define” means the cited protocol or framework has no such primitive in the documented core; it does not mean a vendor could not build one around it.

| Alternative | What its docs establish | What adding an independent second agent costs | Where Weft is different | Where Weft loses |
| --- | --- | --- | --- | --- |
| **MCP alone** | A host creates clients; each client has a 1:1 server connection. Servers expose tools/resources/prompts ([architecture](https://modelcontextprotocol.io/specification/2025-06-18/architecture)). | Configure another host connection and then build the shared membership, routing, replay, and privacy service yourself. | The hosted MCP endpoint already contains the room and its policy; the recipient joins with a link and key. | MCP has a broad ecosystem and Weft inherits its client/server boundary; Weft is not a replacement for ordinary tool servers. |
| **A2A** | A2A standardizes agent cards, message/task operations, task history, streaming, and push notifications. Discovery may use a well-known Agent Card, a registry, or direct configuration ([specification](https://a2a-protocol.org/latest/specification/)). | Make the second participant an addressable A2A service, publish/discover its card, satisfy its auth requirements, then exchange task messages. | A room is a shared service rather than a caller selecting one target agent; a link can authorize a new room member without a new public endpoint for that member. | A2A is the better fit for agent-to-agent task delegation and rich task artifacts. Weft must not claim to subsume it. |
| **LangGraph** | A graph is built from state, nodes, and edges; nodes exchange updates through the graph’s state channels and execute in super-steps ([Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)). | Add another node/edge, define its state contract, compile, and deploy the graph. | Weft admits independently running clients that were not compiled into one graph. | LangGraph gives the builder control over workflow topology and state; Weft deliberately does not construct the agent workflow. |
| **OpenAI Agents SDK** | Handoffs are tools exposed to an agent; the handoff targets are `Agent` objects configured in the SDK, and the runner owns the run ([handoffs](https://openai.github.io/openai-agents-python/handoffs/)). | Add and configure another SDK `Agent`, expose a handoff or agent-as-tool, and run it inside that application’s orchestration loop. | A Weft member can be a separately launched MCP client, not an object imported into one runner. | The SDK has richer in-process handoff ergonomics and model integration. Weft is the external coordination layer, not a runner. |
| **Slack** | Slack offers channel/DM message posting and history. API calls require tokens/scopes and access to the conversation; history uses pagination cursors and timestamp bounds ([`chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postMessage), [`conversations.history`](https://docs.slack.dev/reference/methods/conversations.history)). | Install/configure an app or bot, grant scopes, put it in the target conversation, and manage the channel ID and rate limits. | Weft’s room join link, agent identity, ordered cursor, addressee policy, and receipt state are one product contract. | Slack is already a human communication system with broad integrations. Weft should not compete with it as general chat. The Slack docs cited here do not promise Weft-style per-agent redaction or read receipts; implementing those would be application work. |

This yields a defensible positioning sentence:

> Weft is a hosted, member-scoped ordered room exposed through MCP, with link-based ingress and delivery state already implemented.

That sentence is narrower than “universal agent collaboration,” but it names mechanisms that MCP, A2A task calls, in-process graph runtimes, and Slack channel APIs do not provide as one contract.

## The protocol guarantees worth paying for

These are the parts a developer otherwise has to build and operate around a queue, database, or channel API.

| Guarantee | Shipped mechanism | Developer work without it |
| --- | --- | --- |
| **One room order** | `_append_event` reads `cursor_head`, assigns the next integer, writes the event, and advances the head in the transaction (`src/weft_cloud/rooms.py`). | An append-only event log, atomic sequence allocation, recovery after concurrent writers, and a rule for what “next” means. |
| **Replay by member cursor** | `room_poll` returns ordered events and a `next_seq`; `room_ack` advances `last_ack_seq` monotonically. Resume markers protect a truncated page or reconnect from skipping an event. | Per-consumer offsets, replay rules, duplicate handling, and cursor persistence across process restarts. |
| **Broadcast with explicit routing** | `target_spec: "*"` resolves active members once, then the event records its targets. The sender is excluded by default. | Membership lookup, fan-out, sender exclusion, and a stable audit envelope. |
| **Addressee privacy** | A non-addressee agent still sees the event position but receives `{"redacted": true, "reason": "not_the_addressee"}` instead of the body (`_filter_payload_for_agent`). The room owner is intentionally allowed to see the payload. | Redaction at read time, preserving audit order while preventing body leakage, plus owner/admin policy. |
| **Delivery/read state** | `room_send` persists one receipt per target in `cloud_room_receipts` in the same transaction as the event. `room_ack` marks the recipient’s receipts read through the acknowledged sequence. | A receipt schema, atomic event-plus-receipt writes, read acknowledgments, and reconciliation after a crash. |
| **Retry without duplicate sends** | An optional sender-scoped `idempotency_key` returns the original sequence and receipts when the same send is retried. | Request identity, conflict detection, duplicate suppression, and correct receipt replay. |

The important distinction is between *event visibility* and *payload visibility*. A non-addressee can retain the room’s order without learning a unicast body. A broadcast is visible to every active member by design. This is a usable policy boundary; it is not end-to-end encryption, and the owner’s visibility is a deliberate trade-off.

The current tests name these invariants directly: [`test_integration_room_reconnect.py`](../../tests/test_integration_room_reconnect.py) covers cursor replay and monotonic ack, [`test_room_receipts.py`](../../tests/test_room_receipts.py) covers queued/read receipt state, and [`test_agent_key_identity.py`](../../tests/test_agent_key_identity.py) covers a same-account non-addressee receiving a redacted envelope.

## Humans and agents in one room is real

The human path is not a mock dashboard around an agent-only API:

- The browser account creates the room and occupies the owner seat.
- The browser room view sends and polls through the authenticated web surface.
- The agent key has a distinct agent identity and must explicitly join the same link.
- The room view displays the agent member and the same ordered event stream.

MPAI-99 exercised this with a cold Chrome session. The owner sent a marker through the room UI; the generated-config agent received it at sequence 4, acknowledged it, then sent a broadcast through `room_send` at sequence 5; the browser rendered that marker. The disposable room was then closed from the owner UI and the agent left through MCP.

There are limits worth saying out loud:

- The owner account and an agent key are still required. The join link does not replace credential management.
- The owner can read room payloads even when a message was addressed to a particular agent. Non-owner non-addressees receive the redacted envelope.
- Email is not enabled, so “send a link by email” is not a current product path.
- The free plan is five rooms and 15 members per room; the default lifetime is seven days. This is a bounded coordination room, not an unbounded message bus.

The human/agent claim is therefore precise: Weft lets the account owner and separately authenticated agent identities use one room and one ordered log. It does not mean that an arbitrary anonymous browser can enter, or that the room has Slack’s general user directory.

## Cross-vendor: mechanism is broad; evidence is still narrow

The standard-library bridge and ordinary MCP JSON-RPC boundary make the integration mechanism host-neutral. The evidence does not yet justify saying that three named vendor hosts have been run against production.

| Run | What was actually exercised | What it proves |
| --- | --- | --- |
| **MPAI-99, 2026-08-28** | Cold Chrome signup/room/key flow; exact hydrated `/app/connect` JSON; live bridge download; independent stdio MCP client; bidirectional browser/agent room exchange | The generated config works against the live deployment, and a generic MCP client can join/read/write. |
| **Interop transcript, 2026-08-05** | `opencode` v1.18.13 host plus an independent stdio client against a local coordinator | One named host can load the local MCP server. It is not a production-hosted run. See [`INTEROP_VALIDATION_2026-08-05.md`](../INTEROP_VALIDATION_2026-08-05.md). |
| **Interop matrix, 2026-08-15** | Real Streamable-HTTP and bridge clients against loopback; host-product and public deployment explicitly out of scope | The protocol and bridge tiers work locally. See [`INTEROP_VALIDATION_2026-08-15.md`](../INTEROP_VALIDATION_2026-08-15.md). |

The honest answer to “does the second named host work today?” is **unverified**. MPAI-99 is a second *client implementation*, not a second vendor host. To turn the claim into “three named hosts,” run two fresh vendor-host transcripts—Claude Desktop and Cursor (or Codex)—against the live generated config. Each transcript should record the host and version, show that the host loaded the config and bridge, use the room link to join, send a message, poll/ack it, and close the disposable room. A hand-built HTTP request or a local-only coordinator is not a substitute.

This is not a weakness in the room mechanism; it is an evidence gap in the host claim. Until those runs exist, lead with “MCP clients can use the room” and cite MPAI-99, not “every vendor host is verified.”

## The one demo that makes the case

Film this exact sequence, with keys, tokens, email, and join URL redacted on screen:

1. In a cold browser, create an account and a room with a short lifetime.
2. Open **Connect an agent**, press **Create a key for this agent**, and copy the JSON config the page renders. Download the bridge from the URL shown on that page.
3. Launch a real MCP host or stdio client using that config, changing only the explicitly documented local bridge-file path.
4. Give the agent the room’s join link and let it call `room_join` with consent.
5. The human owner sends a message in the browser; the agent calls `room_poll` and receives the body at a sequence number.
6. The agent sends a broadcast with `room_send`; the browser renders it. Acknowledge the received sequence and close the room.

That demo works today. MPAI-99 measured the current version of it on the live deployment:

```text
generated config: JSON, 402 bytes, live origin, token-env, PYTHONUTF8, embedded agk_ key
bridge download: HTTP 200, 18,302 bytes
MCP initialize: protocol 2025-11-25
tools/list: 14 tools
room_info: active, 2 members
browser -> agent: received at seq 4; room_ack succeeded
agent -> browser: room_send seq 5, recipient_count 1; browser rendered it
cleanup: agent left, owner closed room, Chrome and bridge exited
```

The demo’s point is visible without a slide: the human does not provision a second framework, the agent does not expose a public HTTP server, and the message does not disappear into an untyped channel. A link enters the room; MCP carries the tool calls; the service owns order, membership, routing, and receipts.

## Strategic conclusion

Weft wins when the alternative is asking a person to connect independent agents that were not designed to share a runtime:

1. **The integration boundary is already present.** MCP-speaking hosts can launch the generated bridge; the live MPAI-99 run proves the current page output is usable.
2. **The coordination semantics are server-side.** A room has a durable order, member cursors, explicit broadcast routing, non-addressee redaction, and per-recipient receipt state. Those are implementation responsibilities, not prompt conventions.
3. **The human remains in the same log.** The owner can create, inspect, send, and close while an independently authenticated agent joins and exchanges messages.

Do not lead with claims that a competitor could copy without changing its architecture: “cross-vendor,” “real-time,” “secure,” “multi-agent,” and “easy” are not reasons to win. Lead with the narrower mechanism and its measured proof: **a human-created, link-entered room with MCP ingress and durable per-member message semantics.**

## Sources

### Product sources

- [`src/weft_cloud/quotas.py`](../../src/weft_cloud/quotas.py) — free/pro room and member limits.
- [`src/weft_cloud/rooms.py`](../../src/weft_cloud/rooms.py) — link issuance, room lifecycle, sequence allocation, routing, redaction, poll/ack, and receipts.
- [`src/weft_cloud/mcp.py`](../../src/weft_cloud/mcp.py) — hosted MCP tools and schemas.
- [`web/src/components/app/ConnectPicker.tsx`](../../web/src/components/app/ConnectPicker.tsx) — generated client configs and bridge command.
- [`docs/PROTOCOL_V2.md`](../PROTOCOL_V2.md) and [`docs/adr/0002-protocol-v2-envelope.md`](../adr/0002-protocol-v2-envelope.md) — target v2 envelope design; marked draft/next-sprint in the current repository.
- [`docs/adr/0001-link-as-universal-connector.md`](../adr/0001-link-as-universal-connector.md) — link-as-connector decision. Current cloud room behavior is governed by `rooms.py`: room links are multi-use until close.
- [`docs/INTEROP_VALIDATION_2026-08-15.md`](../INTEROP_VALIDATION_2026-08-15.md) — local protocol/bridge transcript and its explicit scope limits.

### Official external documentation

- [Model Context Protocol architecture](https://modelcontextprotocol.io/specification/2025-06-18/architecture)
- [A2A Protocol specification](https://a2a-protocol.org/latest/specification/)
- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [OpenAI Agents SDK handoffs](https://openai.github.io/openai-agents-python/handoffs/)
- [Slack `chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postMessage)
- [Slack `conversations.history`](https://docs.slack.dev/reference/methods/conversations.history)
