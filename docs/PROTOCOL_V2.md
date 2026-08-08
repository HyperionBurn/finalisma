# Weft Coordination Protocol — weft.a2a/2.0

**Status:** Draft specification for next-sprint implementation.
**Supersedes:** `docs/PROTOCOL.md` (weft.a2a/1.0) for all features defined here. 1.0 remains valid for unicast pairing and task lifecycle; 2.0 is additive.

## 1. Design goals and scope

Weft 2.0 extends the coordination layer from a strict two-party pairing model to an **N-way, multi-agent, multi-adapter** fabric. The invariants of 1.0 (attributable mutations, atomic fencing-token claims, idempotency, evidence-gated completion, no arbitrary command execution) remain inviolate. 2.0 adds:

1. **Multi-recipient addressing** — one envelope reaches many agents.
2. **Capability negotiation** — explicit offer/accept/reject/renegotiate with auditable degradation.
3. **Discovery** — roster, capability query, presence, scoped addressing.
4. **Adapter-awareness** — the envelope records which bridge tier carried it, so capability guarantees are enforceable.
5. **N-way session lifecycle** — member join, topology change, partition/reconnect, leave.

The protocol remains transport-neutral: MCP/stdio, MCP/HTTP, and the bridge tiers described below are adapters at the edge.

## 2. Envelope v2

### 2.1 Canonical shape

`send_message` and the session event store persist this shape for v2 envelopes. All new fields are optional-by-default so a 1.0 envelope remains valid (see §6).

```json
{
  "protocol": "weft.a2a",
  "version": "2.0",
  "message_id": "msg_01J9X...",
  "type": "task.dispatch",
  "task_id": "task_01J9X...",
  "sender": {
    "agent_id": "agent-a",
    "model": "gpt-5.6-luna"
  },
  "recipient": { "agent_id": "agent-b" },
  "targets": [
    { "agent_id": "agent-b" },
    { "agent_id": "agent-c" }
  ],
  "roster_id": "rost_01J9X...",
  "timestamp": "2026-08-05T12:00:00.000Z",
  "correlation_id": "task_01J9X...",
  "payload": { "title": "Implement auth module", "description": "..." },
  "capabilities_declared": ["coding", "testing", "long-context"],
  "capabilities_required": ["coding"],
  "hop_count": 0,
  "adapter": "mcp/stdio",
  "trace_id": null,
  "signature": null,
  "priority": 2,
  "idempotency_key": "idem_01J9X..."
}
```

### 2.2 Field definitions

| Field | Type | 1.0 | 2.0 | Description |
| --- | --- | --- | --- | --- |
| `protocol` | `string` | ✅ | ✅ | MUST be `"weft.a2a"`. |
| `version` | `string` | ✅ | ✅ | `"1.0"` or `"2.0"`. Receivers MUST accept both. |
| `message_id` | `string` | ✅ | ✅ | Unique message id, prefix `msg_`. |
| `type` | `string` | ✅ | ✅ | Message type (see §2.4). |
| `task_id` | `string\|null` | ✅ | ✅ | Task this message concerns, if any. |
| `sender` | `object` | ✅ | ✅ | `{ agent_id, model }`. |
| `recipient` | `object` | ✅ | ✅ | **Unicast target** — single object `{ agent_id }` or `{ scope: "broadcast" }`. Retained for backward compatibility. |
| `targets` | `array` | — | 🆕 | **Multi-recipient list.** Each entry is `{ agent_id }`. When present, the envelope is multi-recipient; `recipient` MUST be omitted or equal to `targets[0]`. |
| `roster_id` | `string\|null` | — | 🆕 | The roster (team+topology group) this message belongs to. Scoped addressing is resolved against this roster. |
| `timestamp` | `string` | ✅ | ✅ | ISO-8601 UTC. |
| `correlation_id` | `string` | ✅ | ✅ | Correlates a thread of messages. |
| `payload` | `object` | ✅ | ✅ | Application data, never server instructions. |
| `capabilities` | `array` | ✅ | — | **Deprecated in 2.0.** Replaced by `capabilities_declared`. 2.0 receivers MUST accept `capabilities` as an alias for `capabilities_declared`. |
| `capabilities_declared` | `array\|null` | — | 🆕 | Capabilities the sender offers for this interaction (manifest). |
| `capabilities_required` | `array\|null` | — | 🆕 | Capabilities the sender requires from the receiver(s) to fulfill the request. |
| `hop_count` | `integer` | — | 🆕 | Number of relay hops since origin. Starts at `0`. Relays MUST increment. Receivers MUST reject when `hop_count > max_hops` (default 4). |
| `adapter` | `string` | — | 🆕 | Bridge tier that carried this envelope (see §2.3). |
| `trace_id` | `string\|null` | ✅ | ✅ | Distributed-trace correlation. |
| `signature` | `string\|null` | ✅ | ✅ | Reserved for signed-message extension. |
| `priority` | `integer` | ✅ | ✅ | 0 (critical) – 3 (low). |
| `idempotency_key` | `string\|null` | — | 🆕 | Per-sender idempotency key. A second message with the same `(team_id, sender_id, idempotency_key)` returns the original result. |

### 2.3 Adapter tiers

The `adapter` field records which bridge carried the envelope. Receivers use it to enforce capability guarantees.

| Value | Tier | Capability constraints |
| --- | --- | --- |
| `"mcp/stdio"` | MCP over stdio (local process) | Full capability set. No size relay. |
| `"mcp/http"` | MCP over Streamable HTTP | Full capability set. |
| `"bridge-webhook"` | Outbound webhook bridge | No streaming; payload ≤ 256 KB; no bidirectional capability negotiation during transit. |
| `"bridge-embed"` | Embedded SDK / in-process bridge | Full capability set; same-process. |
| `"bridge-cli"` | CLI / headless bridge | No streaming; capabilities limited to what the CLI host exposes. |

A receiver that cannot carry a capability declared by the sender MUST follow the degradation rules in §4 — never silently drop.

### 2.4 Message types (2.0 additions)

1.0 types remain valid. 2.0 adds:

| Type | Direction | Purpose |
| --- | --- | --- |
| `capability.offer` | sender → receiver(s) | Offer a capability manifest. |
| `capability.accept` | receiver → sender | Accept the offer (possibly degraded). |
| `capability.reject` | receiver → sender | Reject with reason. |
| `capability.renegotiate` | either | Request revised terms. |
| `discovery.roster_query` | any → coordinator | Request roster listing. |
| `discovery.roster_response` | coordinator → any | Roster listing reply. |
| `discovery.capability_query` | any → coordinator | Query an agent's declared capabilities. |
| `discovery.capability_response` | coordinator → any | Capability query reply. |
| `discovery.presence` | coordinator → any | Heartbeat-derived presence update. |
| `roster.join` | new agent → coordinator | Request to join a roster. |
| `roster.leave` | agent → coordinator | Graceful leave. |
| `roster.topology` | coordinator → members | Topology-change notification. |
| `evidence.submit` | agent → coordinator | Submit evidence for a task (multi-recipient-aware). |

## 3. N-way semantics

### 3.1 Addressing modes

| Mode | `recipient` | `targets` | Semantics |
| --- | --- | --- | --- |
| Unicast | `{ agent_id: "X" }` | omitted or `[{ agent_id: "X" }]` | One recipient. 1.0-compatible. |
| Multicast (fan-out) | omitted or `targets[0]` | `[...]` (≥2 entries) | One message, multiple recipients. Each gets an independent delivery record. |
| Broadcast | `{ scope: "broadcast" }` | omitted | All active members of the roster. |
| Group reply | `{ agent_id: "X" }` | omitted | Reply to sender `X` on a correlation thread; roster members observe. |

### 3.2 Idempotency rules per recipient

- The **origin** `idempotency_key` is enforced globally: a second envelope with the same `(team_id, sender_id, idempotency_key)` returns the original result without re-delivery.
- For **multicast**, each target gets a per-recipient delivery record keyed by `(message_id, agent_id)`. A re-delivery to a specific agent (e.g. after reconnect) is idempotent per recipient.
- **Broadcast** delivery is best-effort; agents that miss a broadcast can replay from the session cursor (see §7).

### 3.3 Delivery guarantees

- Unicast: at-least-once, ordered per `(sender, recipient)` pair via session cursors.
- Multicast: at-least-once per recipient, independent per-recipient cursors.
- Broadcast: at-most-once, no per-recipient ordering guarantee; agents SHOULD use `discovery.presence` to detect gaps.

## 4. Capability negotiation handshake

### 4.1 Purpose

The "no capability loss" guarantee: when an adapter or receiver cannot carry a capability, degradation is **explicit and auditable**, never silent.

### 4.2 Handshake flow

```
                              capability.offer
   Sender ──────────────────────────────────────► Receiver(s)
    │                                              │
    │  capability.accept  ◄── full match           │
    │  capability.accept  ◄── degraded (with       │
    │                       degradation manifest)   │
    │  capability.reject  ◄── cannot satisfy       │
    │                       capabilities_required   │
    │                                              │
    │  capability.renegotiate ◄────────────────────►  (either side)
```

### 4.3 Offer

A `capability.offer` envelope carries:

```json
{
  "type": "capability.offer",
  "capabilities_declared": ["coding", "testing", "long-context"],
  "capabilities_required": ["coding"],
  "adapter": "mcp/stdio"
}
```

- `capabilities_declared` — what the sender can provide.
- `capabilities_required` — what the sender needs from the receiver to proceed.

### 4.4 Accept

```json
{
  "type": "capability.accept",
  "capabilities_declared": ["coding", "testing"],
  "capabilities_accepted": ["coding", "testing"],
  "degradation": null
}
```

If the receiver can satisfy all `capabilities_required`, `capabilities_accepted` includes them and `degradation` is `null`.

### 4.5 Degradation rules

When the receiver (or the adapter) cannot carry a capability:

1. **Required capability missing** → MUST `capability.reject` with a `reason` and the missing capability list.
2. **Optional capability missing** → MAY `capability.accept` with a `degradation` manifest:

```json
{
  "type": "capability.accept",
  "capabilities_declared": ["coding"],
  "capabilities_accepted": ["coding"],
  "degradation": {
    "dropped": ["long-context"],
    "reason": "adapter bridge-cli caps context at 32k tokens",
    "adapter": "bridge-cli"
  }
}
```

3. **Adapter-forced degradation** — when the adapter itself cannot carry a capability (e.g. `bridge-webhook` cannot stream), the receiver MUST include a `degradation` manifest with `reason` and `adapter` fields. The sender logs this as an auditable degradation event.
4. **Silent degradation is forbidden.** A receiver that drops a capability without declaring it in `degradation` is in protocol violation.

### 4.6 Reject

```json
{
  "type": "capability.reject",
  "reason": "missing-required",
  "missing_capabilities": ["long-context"],
  "adapter": "bridge-cli"
}
```

### 4.7 Renegotiate

Either side MAY send `capability.renegotiate` with a revised `capabilities_declared` / `capabilities_required`. The handshake restarts from the offer. Implementations SHOULD bound renegotiation to 3 rounds to prevent livelock.

## 5. Discovery

### 5.1 Roster listing

`discovery.roster_query` requests the current roster. The coordinator replies with `discovery.roster_response`:

```json
{
  "type": "discovery.roster_response",
  "roster_id": "rost_01J9X...",
  "members": [
    {
      "agent_id": "agent-a",
      "name": "Planner",
      "role": "architect",
      "status": "active",
      "last_seen": "2026-08-05T11:58:00.000Z",
      "capabilities": ["planning", "critique", "coding", "research"],
      "adapter": "mcp/stdio"
    }
  ]
}
```

### 5.2 Capability query

`discovery.capability_query` with `{ agent_id: "X" }` returns the declared capabilities of agent X as of the last registration/heartbeat.

### 5.3 Presence

Presence is heartbeat-derived. An agent is:

- **active** — `last_seen` within `heartbeat_timeout`.
- **stale** — `last_seen` exceeds `heartbeat_timeout` but less than `2 × heartbeat_timeout`.
- **absent** — `last_seen` exceeds `2 × heartbeat_timeout`.

The coordinator emits `discovery.presence` events on state transitions (active→stale, stale→absent, absent→active).

### 5.4 Addressing

`agent_id` is scoped to `(team_id, roster_id)`. Two agents in different rosters MAY share an `agent_id` without collision. Unicast addressing MUST include `roster_id` when the envelope is 2.0; the coordinator resolves `agent_id` within that roster.

## 6. Versioning and backward compatibility

### 6.1 Envelope compatibility

| Scenario | Behavior |
| --- | --- |
| 1.0 sender → 2.0 receiver | Receiver accepts. Missing 2.0 fields default to `null`/empty. `capabilities` is read as `capabilities_declared`. |
| 2.0 sender → 1.0 receiver | Receiver ignores unknown fields (standard JSON-RPC forward compatibility). If the envelope has `targets` (multi-recipient), the 1.0 receiver processes `recipient` only. |
| Mixed roster | The coordinator stores both versions. Session cursors are version-agnostic. |

### 6.2 Feature gating

Features that require 2.0 are marked **"requires v2"** throughout this document. A 1.0-only agent in a 2.0 roster:

- CAN participate in unicast pairing, task lifecycle, evidence gate.
- CANNOT be a multicast target (the coordinator delivers unicast only).
- CANNOT participate in capability negotiation (the coordinator treats its capabilities as static, declared at registration).

### 6.3 Version negotiation

On roster join, the agent declares its maximum supported version. The coordinator records `agent_version`. If all members support 2.0, the coordinator enables 2.0 features for that roster. If any member is 1.0, multicast and capability negotiation are disabled for the roster until all members upgrade.

## 7. Wire examples

### 7.1 Pairing (1.0-compatible, unchanged)

```json
{
  "protocol": "weft.a2a",
  "version": "1.0",
  "message_id": "msg_pair_001",
  "type": "pairing.create",
  "sender": { "agent_id": "agent-a", "model": "gpt-5.6-luna" },
  "recipient": { "agent_id": "agent-b" },
  "timestamp": "2026-08-05T12:00:00.000Z",
  "correlation_id": "pair_001",
  "payload": { "join_url": "https://host/pair#token=..." },
  "capabilities": ["read", "comment"],
  "trace_id": null,
  "signature": null,
  "priority": 2
}
```

### 7.2 Task dispatch (2.0 multicast)

```json
{
  "protocol": "weft.a2a",
  "version": "2.0",
  "message_id": "msg_dispatch_001",
  "type": "task.dispatch",
  "task_id": "task_01J9X...",
  "sender": { "agent_id": "agent-a", "model": "gpt-5.6-luna" },
  "targets": [
    { "agent_id": "agent-b" },
    { "agent_id": "agent-c" }
  ],
  "roster_id": "rost_01J9X...",
  "timestamp": "2026-08-05T12:00:00.000Z",
  "correlation_id": "task_01J9X...",
  "payload": { "title": "Implement auth", "description": "..." },
  "capabilities_declared": ["coding", "testing"],
  "capabilities_required": ["coding"],
  "hop_count": 0,
  "adapter": "mcp/stdio",
  "trace_id": null,
  "signature": null,
  "priority": 1,
  "idempotency_key": "idem_dispatch_001"
}
```

### 7.3 Capability negotiation

**Offer:**
```json
{
  "protocol": "weft.a2a",
  "version": "2.0",
  "message_id": "msg_cap_offer_001",
  "type": "capability.offer",
  "sender": { "agent_id": "agent-a", "model": "gpt-5.6-luna" },
  "recipient": { "agent_id": "agent-b" },
  "timestamp": "2026-08-05T12:00:01.000Z",
  "correlation_id": "task_01J9X...",
  "payload": {},
  "capabilities_declared": ["coding", "testing", "long-context"],
  "capabilities_required": ["coding"],
  "hop_count": 0,
  "adapter": "mcp/stdio",
  "trace_id": null,
  "signature": null,
  "priority": 1
}
```

**Accept (degraded):**
```json
{
  "protocol": "weft.a2a",
  "version": "2.0",
  "message_id": "msg_cap_accept_001",
  "type": "capability.accept",
  "sender": { "agent_id": "agent-b", "model": "longcat/LongCat-2.0" },
  "recipient": { "agent_id": "agent-a" },
  "timestamp": "2026-08-05T12:00:02.000Z",
  "correlation_id": "task_01J9X...",
  "payload": {},
  "capabilities_declared": ["coding", "testing"],
  "capabilities_accepted": ["coding", "testing"],
  "degradation": {
    "dropped": ["long-context"],
    "reason": "adapter bridge-cli caps context at 32k tokens",
    "adapter": "bridge-cli"
  },
  "hop_count": 0,
  "adapter": "bridge-cli",
  "trace_id": null,
  "signature": null,
  "priority": 1
}
```

### 7.4 Evidence submission (multi-recipient-aware)

```json
{
  "protocol": "weft.a2a",
  "version": "2.0",
  "message_id": "msg_evidence_001",
  "type": "evidence.submit",
  "task_id": "task_01J9X...",
  "sender": { "agent_id": "agent-b", "model": "longcat/LongCat-2.0" },
  "recipient": { "agent_id": "agent-a" },
  "timestamp": "2026-08-05T12:05:00.000Z",
  "correlation_id": "task_01J9X...",
  "payload": {
    "checks": [
      { "name": "unit-tests", "status": "passed" },
      { "name": "lint", "status": "passed" }
    ],
    "artifacts": ["src/auth.py", "tests/test_auth.py"],
    "per_target": {
      "agent-a": { "artifacts": ["src/auth.py"] },
      "agent-c": { "artifacts": ["tests/test_auth.py"] }
    }
  },
  "capabilities_declared": ["testing"],
  "hop_count": 0,
  "adapter": "mcp/stdio",
  "trace_id": null,
  "signature": null,
  "priority": 1
}
```

### 7.5 Reconnect / replay

On reconnect, an agent calls `session_poll` with its last `last_ack_seq`. The coordinator replays `session_events` with `seq > last_ack_seq`, preserving order. The agent MUST acknowledge with `session_ack` after durable processing.

## 8. State machine — N-way session lifecycle

### 8.1 States

| State | Meaning |
| --- | --- |
| `forming` | Roster created, members joining. |
| `active` | Roster has ≥2 active members; normal operation. |
| `degraded` | One or more members stale; operations continue with reduced quorum. |
| `partitioned` | Network split; each partition operates independently until healed. |
| `recovering` | Partition healing; replay and reconciliation in progress. |
| `closing` | Graceful shutdown initiated. |
| `closed` | Roster dissolved; no further messages. |

### 8.2 Transitions

| From | Event | To | Side effects |
| --- | --- | --- | --- |
| `forming` | ≥2 members active | `active` | Enable 2.0 features if all members support it. |
| `forming` | timeout (no second member) | `closed` | Emit `roster.expired`. |
| `active` | member becomes stale | `degraded` | Emit `roster.topology` with stale member. |
| `active` | new member joins | `active` | Emit `roster.topology`; replay recent events to new member. |
| `active` | member leaves gracefully | `active` | Emit `roster.topology`; redistribute owned tasks to `pending`. |
| `active` | partition detected | `partitioned` | Each side continues with available members. |
| `degraded` | stale member returns | `active` | Replay missed events. |
| `degraded` | member leaves | `active` or `degraded` | Depends on remaining member count. |
| `partitioned` | connectivity restored | `recovering` | Reconcile divergent state via session cursors. |
| `recovering` | reconciliation complete | `active` | Emit `roster.topology` with healed state. |
| `active` | graceful close requested | `closing` | No new tasks; finish in-flight. |
| `closing` | all members ack close | `closed` | Persist final audit trail. |
| `closing` | timeout | `closed` | Force-close; emit `roster.force_closed`. |

### 8.3 Member join

1. New agent calls `roster.join` with `roster_id`, `capabilities_declared`, `adapter`.
2. Coordinator validates, adds member, emits `roster.topology`.
3. New member receives replay of recent `session_events` (bounded to last 1000 events or 24h).
4. If all members now support 2.0, coordinator enables 2.0 features.

### 8.4 Roster topology change

Any join, leave, or stale transition emits a `roster.topology` broadcast:

```json
{
  "type": "roster.topology",
  "roster_id": "rost_01J9X...",
  "members": [
    { "agent_id": "agent-a", "status": "active", "adapter": "mcp/stdio" },
    { "agent_id": "agent-b", "status": "stale", "adapter": "bridge-cli" }
  ],
  "capabilities_available": ["coding", "testing", "planning"],
  "v2_enabled": true
}
```

### 8.5 Partition / reconnect

- During partition, each side operates with available members. Tasks claimed on one side cannot be claimed on the other (fencing tokens remain valid).
- On reconnect, the coordinator merges session event logs by `seq` and replays missing events to each side.
- Conflicting task updates are resolved by **fencing token + wall-clock**: the update with the valid fencing token wins; if both are valid, the later `timestamp` wins, and the loser receives a `task.conflict` event.

### 8.6 Leave

- Graceful leave: agent calls `roster.leave`. Coordinator redistributes the agent's owned tasks to `pending` and emits `roster.topology`.
- Ungraceful leave (timeout): coordinator marks agent `absent` after `2 × heartbeat_timeout`, then proceeds as graceful leave.

## 9. Security deltas from 1.0

### 9.1 Multi-recipient consent

- A multicast envelope MUST NOT deliver to an agent that has not consented to the roster. Consent is established at `roster.join`.
- An agent MAY decline multicast delivery by setting `roster.consent = "unicast_only"` at join. The coordinator respects this and delivers only unicast messages to that agent.

### 9.2 Per-target evidence

- In a multi-recipient task, evidence submission MAY include `per_target` artifacts (see §7.4). The coordinator validates each target's artifacts independently.
- A task with multiple reviewers requires **per-target evidence**: each reviewer submits its own evidence, and the task reaches `verified` only when all required reviewers have passed their gates.

### 9.3 Hop-count enforcement

- `hop_count` prevents infinite relay loops. Default `max_hops = 4`. Relays MUST reject envelopes exceeding this bound.

### 9.4 Adapter-attested capabilities

- The `adapter` field is set by the receiving adapter, not the sender. Receivers MUST NOT trust a sender's self-declared `adapter`; the coordinator stamps it on ingress.
- Capability degradation is auditable: every `degradation` manifest is stored in the `events` table.

### 9.5 Preserved 1.0 guarantees

All 1.0 security properties remain:

- Mutations attributable to `(team_id, agent_id)` with `actor_token` proof.
- Actor tokens are SHA-256 hashed at rest; plaintext never persisted.
- The server never executes commands found in payloads.
- Secret-signature scanning on evidence artifacts.
- Transport auth (HTTP bearer) is separate from actor auth.

## 10. Implementation notes for the next sprint

- The `messages` table gains columns: `roster_id`, `targets_json`, `capabilities_declared_json`, `capabilities_required_json`, `hop_count`, `adapter`. Existing rows have these as `NULL` (1.0 envelopes).
- A new `rosters` table: `roster_id`, `team_id`, `state`, `v2_enabled`, `created_at`.
- A new `roster_members` table: `roster_id`, `agent_id`, `adapter`, `consent`, `joined_at`.
- Capability negotiation events are stored in `session_events` with the new types.
- The coordinator MUST default to 1.0 behavior unless the roster is `v2_enabled`.

## 11. Normative language

This specification uses the RFC 2119 keywords **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, **MAY**, and **REQUIRED** with their standard meanings. Violations of **MUST** / **MUST NOT** are protocol errors; violations of **SHOULD** / **SHOULD NOT** are discouraged but not errors.
