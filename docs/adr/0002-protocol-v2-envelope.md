# ADR-0002 — Protocol v2 Envelope (finalisma.a2a/2.0)

**Status:** Accepted (draft for next-sprint implementation)
**Supersedes:** ADR-0001 (implicit — the original protocol design)
**Author:** PROTOCOL-V2 lane
**Date:** 2026-08-05

## Context

Finalisma 1.0 (`docs/PROTOCOL.md`) defines a two-party, unicast pairing protocol with a message envelope, task lifecycle, and evidence gate. It works for two MCP-capable agents sharing a state file. The vision for Finalisma as a next-gen multiplayer AI coordination layer requires:

- **N-way communication** — one agent addressing many, not just one.
- **Multi-adapter support** — MCP/stdio, MCP/HTTP, webhook bridges, embedded SDKs, and CLI bridges.
- **Explicit capability negotiation** — so adapters that cannot carry a capability degrade auditable rather than silently.
- **Discovery** — roster, presence, capability query, scoped addressing.
- **Graceful coexistence** with 1.0, which is already shipped.

The v1.0 envelope has no multi-recipient field, no capability negotiation, no adapter tag, and no discovery message types. Adding these without a version bump would break existing 1.0 receivers that reject unknown fields (or worse, misinterpret them).

## Decision

We introduce **finalisma.a2a/2.0** as an additive envelope version, specified in `docs/PROTOCOL_V2.md`. The key decisions are:

### 1. Additive envelope, optional-by-default new fields

New fields (`targets`, `roster_id`, `capabilities_declared`, `capabilities_required`, `hop_count`, `adapter`, `idempotency_key`) are optional. A 1.0 envelope is a valid 2.0 envelope with all new fields absent. Receivers MUST accept both versions.

**Rationale:** Zero-downtime upgrade. Existing 1.0 agents continue to work; 2.0 features activate only when all roster members support v2.

### 2. `targets[]` for multi-recipient, `recipient` retained

We keep `recipient` for backward compatibility and add `targets[]` for multicast. When `targets` is present, it is authoritative; `recipient` MUST equal `targets[0]` or be omitted.

**Rationale:** Retaining `recipient` avoids breaking every 1.0 tool call. The dual-field approach lets 1.0 receivers read `recipient` and 2.0 receivers read `targets`.

### 3. Capability negotiation as a handshake, not a one-shot tag

Capabilities are negotiated via `capability.offer` / `accept` / `reject` / `renegotiate` message types with a `degradation` manifest. Silent degradation is a protocol violation.

**Rationale:** The "no capability loss" guarantee requires auditability. A one-shot tag cannot express "I wanted long-context but the bridge capped me at 32k." The handshake makes degradation explicit and storable in the audit trail.

### 4. Adapter field stamped by the receiver, not the sender

The `adapter` field records which bridge tier carried the envelope. It is set by the receiving adapter on ingress, not trusted from the sender.

**Rationale:** Prevents an agent from falsely claiming `mcp/stdio` capabilities when it is actually behind a `bridge-cli` that cannot stream. The coordinator stamps the truth.

### 5. Roster as the unit of N-way coordination

A `roster` is a group of agents within a team. `agent_id` is scoped to `(team_id, roster_id)`. Discovery, capability negotiation, and multicast are roster-scoped.

**Rationale:** Teams may have multiple independent coordination groups (e.g. a "frontend" roster and a "backend" roster). Scoping prevents cross-roster collisions and keeps multicast bounded.

### 6. Version gating per roster

2.0 features (multicast, capability negotiation) are enabled only when **all** roster members support v2. If any member is 1.0, the roster operates in 1.0 mode.

**Rationale:** Avoids the "lowest common denominator is implicit" problem. The roster explicitly knows its capability level and can trigger an upgrade prompt when the last 1.0 member is asked to upgrade.

### 7. Idempotency key added to the envelope

A top-level `idempotency_key` enables exactly-once semantics for message creation, complementing the existing per-task idempotency.

**Rationale:** Multi-recipient dispatch and capability negotiation need idempotency beyond task creation. A duplicate `capability.offer` should not restart the handshake.

## Consequences

### Positive

- **N-way coordination** is now spec'd and implementable in one sprint.
- **Multi-adapter** support opens non-MCP hosts (ChatGPT-like, CLI, embedded) without silent capability loss.
- **Auditability** of degradation satisfies the "no capability loss" product guarantee.
- **Backward compatibility** is preserved: 1.0 agents work unchanged.

### Negative / Costs

- The `messages` table gains 7 nullable columns. Migration is additive (no renames), so it is safe but must be tested.
- The dual `recipient` / `targets` fields create a subtle invariant (`recipient == targets[0]` when both present) that implementers must enforce.
- Capability negotiation adds round-trips. Bounded to 3 renegotiation rounds to prevent livelock.
- Per-roster version gating means a single 1.0 straggler disables 2.0 for the whole roster. This is intentional but may frustrate early adopters.

## Alternatives considered

### Alternative A: New protocol, clean break

Drop 1.0 support and define 2.0 as a clean-slate protocol. **Rejected:** Breaks shipped users; the product is not big enough to force a hard migration.

### Alternative B: Capabilities as a single integer bitmask

Encode capabilities as a bitmask for compactness. **Rejected:** Capabilities are user-defined strings (e.g. `"long-context"`, `"coding"`), not a closed enum. A bitmask would require a central registry and prevent extensibility.

### Alternative C: Multicast via repeated unicast

Implement multicast as N unicast sends at the adapter layer, no `targets` field. **Rejected:** Loses per-recipient idempotency, ordering, and auditability. A single multicast envelope with per-recipient delivery records is strictly more powerful.

### Alternative D: Presence as a separate polling API

Require agents to poll `discovery.presence` on an interval. **Rejected:** Heartbeat-derived presence is already implied by `last_seen`. Polling adds load; push on state transition is sufficient.

## Migration plan

1. Add new columns to `messages` table (additive, nullable).
2. Add `rosters` and `roster_members` tables.
3. Implement 2.0 envelope parsing with 1.0 fallback.
4. Implement discovery message types.
5. Implement capability negotiation handshake.
6. Implement multicast delivery with per-recipient cursors.
7. Gate 2.0 features behind roster version check.
8. Smoke-test: 1.0-only roster, 2.0-only roster, mixed roster.

## References

- `docs/PROTOCOL.md` — 1.0 specification (preserved).
- `docs/PROTOCOL_V2.md` — 2.0 specification (this ADR accompanies it).
- `src/finalisma_mcp/core.py` — current implementation (1.0).
- README.md — product overview and quickstart.
