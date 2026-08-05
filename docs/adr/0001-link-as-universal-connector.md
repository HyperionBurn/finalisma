# ADR-0001: Link as universal connector

> **Status:** Accepted.
> **Context:** Finalisma ships a single-node MCP coordinator. Pairing links already
> carry a public pairing ID in the path and a one-time secret in the URL fragment.
> The product vision is a universal agent interconnect: any agent (CLI, app,
> browser/ChatGPT-like, opencode) shares ONE link to connect to any other
> agent(s), N-way, without capability loss. Today only MCP-capable hosts can use it.

## Decision

**The Finalisma pairing link is the single universal connector.** One link format
binds any two hosts regardless of MCP support. We implement three bridging tiers
over the same link + consent flow already shipped in `server.py`:

- **T1 Native MCP** — direct use (shipped).
- **T2 HTTP bridge** (`bridge.py`) — REST adapter over `core.py` for hosts with
  HTTP but no MCP.
- **T3 Browser embed** (`finalisma_sdk`) — JS widget driving the existing
  `/v1/join/:id` endpoints for browser/ChatGPT-like UIs.

Every tier performs the same `pairing_preview → consent=true → join` sequence and
ends with the same `(agent_id, actor_token, session_token)` triple. No tier gets
a shortcut past the consent gate.

## Rationale

1. **The link already has the right security properties.** Public ID in path,
   secret in fragment (never in logs or `Referer`), one-use, preview-before-
   consent, type-strict boolean consent. This is the universal onboarding
   primitive — we do not need a new one.
2. **The HTTP server already supports T3.** `_MCPRequestHandler` serves
   `GET /v1/join/:id` (preview) and `POST /v1/join/:id` (join with token in body).
   A browser embed is implementable today against shipped code.
3. **MCP is an edge adapter, not the product.** `server.py` is a thin adapter
   over `core.py`. Adding a second adapter (`bridge.py`) for non-MCP hosts
   preserves the transport-neutral core while expanding the addressable surface.
4. **Reuse beats re-implement.** The existing `sessions`, `session_credentials`,
   `session_events`, `agents`, `evidence`, `tasks`, and `pairing` tables support
   N-agent groups without schema changes in P0.

## Consequences

- **Positive:** One link works for every host kind. The activation event
  ("Agent A creates a link in under 30 seconds, Agent B joins in under 60
  seconds") is identical for T1/T2/T3. The product becomes the universal
  interconnect without forking the protocol.
- **Positive:** Backward compatibility. Existing MCP clients keep working.
  `bridge.py` and `finalisma_sdk` are additive — no change to `core.py` in P0.
- **Negative / cost:** Three surfaces to maintain and test (MCP, REST, browser).
  Each tier must enforce the same consent + evidence invariants. Test matrix
  grows: every tier needs pairing-join, evidence-gate, and credential-rotation
  coverage.
- **Negative / cost:** `bridge.py` duplicates some dispatcher logic from
  `server.py` (`_assert_capability_scope`, team boundary, rate limiting). This
  duplication is intentional (separate edge adapters over one core) but must be
  kept in sync. `SECURITY_GATES.md` is the shared contract.
- **Risk:** T3 browser hosts are more exposed to credential theft (browser
  storage is weaker than host secret storage). Mitigation: session tokens for
  browser hosts are short-lived and scoped; the SDK stores them in
  `sessionStorage` (not `localStorage`), cleared on tab close. Actor tokens for
  browser hosts are optional in P0 (trusted embed context) and required in P2.
- **Risk:** N-way sessions (P1) require generalizing `agent_a`/`agent_b` in the
  `sessions` table. Mitigation: keep those columns for backward compatibility;
  `session_credentials` (already keyed by `(session_id, agent_id)`) holds all
  members.

## Related

- `docs/NEXUS_ARCHITECTURE.md` — full architecture blueprint (this ADR is the
  top-level decision it implements).
- `docs/SECURITY_GATES.md` — the gate that must pass before multi-instance
  hosted traffic (P2).
- `docs/PROTOCOL.md` — the `finalisma.a2a/1.0` envelope; `2.0` in NEXUS adds
  roster, capability manifest, addressing.
- `src/finalisma_mcp/server.py` — existing HTTP join flow (`/v1/join/:id`).
- `src/finalisma_mcp/core.py` — transport-neutral store reused by all tiers.
