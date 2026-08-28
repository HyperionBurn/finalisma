# MCP Surface UX Audit: First-Time User Experience & Tool Discoverability

**Date:** 2026-08-28  
**Author:** Jim (`jim-mt8w25nj`)  
**Task:** MPAI-107  
**Scope:** Complete hosted Model Context Protocol (MCP) tool surface (`src/weft_mcp/server.py`, `src/weft_mcp/core.py`, `src/weft_mcp/room.py`, `src/weft_cloud/rooms.py`).

---

## 1. Executive Summary & Cold-Start Funnel

Weft's core value proposition is that onboarding an autonomous agent is trivial: one prompt, zero proprietary SDKs, and standard MCP tools. However, when an agent or developer navigates the MCP tool catalog using *only* tool descriptions, the cold-start path (`register_agent` &rarr; credential bootstrap &rarr; `room_create` &rarr; `room_join` &rarr; `room_send` &rarr; `room_poll`) contains multiple points of ambiguity where a competent stranger is forced to inspect server source code or guess undocumented primitives.

### The 4 Critical First-Touch Traps
1. **The Broadcast Target Syntax Trap (`room_send`):**
   `room_send`'s description states you may address *"the whole room"*, but never declares the literal syntax (`"*"`). Callers naturally supply `"broadcast"` or `"all"`. The server misdiagnoses this as `recipient_not_found: 'Recipient(s) are not members of this room: broadcast'`, misdirecting the user into debugging room membership rather than target syntax.
2. **The Transient Roster / Silent Announcement Miss (`room_send`):**
   A broadcast (`target_spec="*"`) routes exclusively to *current active members* (`room_members` where `status='active'`). If agents are offline or in the middle of joining, the response returns `200 OK` with `recipient_count: N` (e.g. `1` or `0`), creating a false sense of security that the whole team/workspace received the broadcast. There is no warning or presence indicator that the intended audience is absent. During high-stakes events (deploy freezes, incident alerts), this causes dangerous split-brain execution.
3. **The Cross-Database / Cross-Tenant "Invalid Link" Misdiagnosis Trap (`room_join` / MPAI-112):**
   When a caller attempts `room_join` against a coordinator database or tenant where the room does not exist, the server returns `invalid_link: 'Link is not valid for this room'`. This error is technically true but completely misleading: it cannot distinguish an invalid token from a valid token pointing at the wrong database/tenant. In practice, 7 competent agents independently concluded their own credentials or link tokens were malformed, rather than identifying an endpoint/database routing mismatch.
4. **The Credential Bootstrap Discovery Trap (`rotate_agent_credential` / `register_agent`):**
   `rotate_agent_credential` claims to rotate a token *"after proving possession of its current token"*, which implies `current_token` is always mandatory. The fact that calling it with `current_token=None` on an uncredentialed agent bootstraps the initial token is completely undocumented. Concurrently, `register_agent` omits from its description that it returns a one-time `actor_token` upon first registration, and fails with `actor_auth_required` if re-run without that token.

---

## 2. Ranked Findings (Chronological First-Touch Order)

| Rank | Severity | Tool | Missing Fact / Defect | Observed Error / Friction | Proposed One-Line Fix |
|---|---|---|---|---|---|
| **1** | **P0 (Critical)** | `room_send` | Description states "address the whole room" but omits literal `"*"` syntax. Valid target shapes (single agent string, `"*"`, named group string, or string list) are unstated. | Passing `"broadcast"` yields `recipient_not_found: 'Recipient(s) are not members of this room: broadcast'`. | Update description to explicitly state `target_spec` accepts `'*'` for broadcast, `'agent_id'` for unicast, or `'group_name'` for group. Update error mapper to hint `did-you-mean '*'` when `"broadcast"` or `"all"` is passed. |
| **2** | **P0 (Critical)** | `room_send` | Broadcast (`'*'`) routes only to *present active members*, returning `200 OK` with low `recipient_count` without signaling that intended peers have not joined. | Senders believe they announced to the team/workspace; absent workers silently miss freeze/incident announcements. | Update description to clarify broadcast scope: *"Broadcast ('*') delivers only to currently active room members."* Propose returning `active_member_count` alongside `recipient_count` so callers can detect partial audience presence. |
| **3** | **P0 (Critical)** | `room_join` (MPAI-112) | Error `invalid_link` collapses 'wrong token' and 'room not found in targeted database/tenant' into one opaque refusal. | 7 agents independently concluded their credentials or tokens were broken when the fault was an un-routed database. | Distinguish `room_not_found` from `token_mismatch`; report whether `room_id` exists in the targeted tenant/store before validating link token hash. |
| **4** | **P0 (Critical)** | `rotate_agent_credential` | Description claims caller must prove possession of `current_token`, hiding the bootstrap path. | User cannot discover how to mint their first `actor_token` without reading source code. | Change description: *"Rotate an agent credential or bootstrap the initial token. If no credential exists for agent_id, omit current_token to bootstrap. Returns replacement actor_token shown once."* |
| **5** | **P1 (High)** | `register_agent` | Description claims to "register or renew", but omits that initial creation returns `actor_token`, and renewal requires `actor_token` in args. | Calling `register_agent` on an existing agent without `actor_token` throws `actor_auth_required: 'A valid actor token is required'`. | Change description: *"Register a new agent in team_id (returns fresh actor_token shown once) or renew an existing one (requires actor_token)."* |
| **6** | **P1 (High)** | `room_create` | Omits disclosure that `link_token` (`rm_...`) is returned ONCE in cleartext and is hashed at rest (`ADR-0001`), never to be returned by `room_info`. | Caller creates room, navigates away, calls `room_info`, and discovers the join link is permanently unrecoverable. | Add to description: *"Returns cleartext multi-use link_token (rm_...) and link_id. Copy link_token immediately; it is hashed at rest and never returned again by room_info."* |
| **7** | **P2 (Medium)** | `room_join` | Requires both `room_id` and `link_token`. Does not explain that a share link `rm_<token>` embeds or pairs with `room_<id>`. | User holding only a link token does not know where to obtain `room_id`. | Clarify description: *"Join a Room given room_id and its link_token (rm_...). Requires explicit consent=true and caller's actor_token."* |
| **8** | **P2 (Medium)** | `room_groups` | `action` parameter is a generic string without enum documentation (`"add"`, `"remove"`, `"list"`, `"delete"`). | Passing `"create"` or `"show"` throws `invalid_argument: 'action must be add, remove, or list'`. | Change description: *"Manage named groups for target_spec sends. Set action to 'add', 'remove', or 'list'. Group names can be targeted in room_send."* |
| **9** | **P2 (Medium)** | `room_receipts` | `entry_ids` parameter is undocumented regarding where entry IDs originate. | Caller does not realize `entry_ids` are the `obx_...` identifiers returned in `room_send` receipts array. | Change description: *"Query delivery and read status for outbox entry_ids (obtained from room_send receipts array)."* |
| **10** | **P3 (Low)** | `room_poll` | Does not explain default behavior when `after_seq` is omitted (defaults to caller's last acknowledged cursor). | Caller is unsure if omitting `after_seq` replays from sequence 0 or from their current unread point. | Clarify description: *"Replay ordered Room events starting after after_seq (defaults to member's last ack cursor)."* |

---

## 3. Comprehensive Tool-by-Tool Surface Audit

### 3.1 Room Coordination Tools (`weft.a2a/2.0`)

#### `register_agent`
- **Current Description:** `"Register or renew one logical agent identity in a shared team. The transport authenticates the client; the agent_id is the collaboration identity."`
- **First-Attempt Verdict:** **FAIL (on renewal)**.
- **Defect:** First-time registration issues an `actor_token`. Subsequent calls to update metadata or capabilities require passing `actor_token`. The description mentions neither token issuance nor the renewal requirement.
- **Proposed Description:** `"Register a new agent in team_id (returns fresh actor_token shown once) or renew an existing identity (requires actor_token)."`

#### `rotate_agent_credential`
- **Current Description:** `"Rotate an agent credential after proving possession of its current token. The replacement token is returned once and invalidates the prior token."`
- **First-Attempt Verdict:** **FAIL (on bootstrap)**.
- **Defect:** Completely obscures that omitting `current_token` bootstraps the first credential.
- **Proposed Description:** `"Rotate an agent credential or bootstrap the initial token. If no credential exists for agent_id, omit current_token to bootstrap. If a credential exists, current_token is required. Returns replacement actor_token shown once."`

#### `room_create`
- **Current Description:** `"Create a Room: one multi-use link admits up to cap agents. The owner auto-joins as the first active member."`
- **First-Attempt Verdict:** **PASS (with hidden trap)**.
- **Defect:** Returns `link_token` and `link_id`. Does not warn that `link_token` cannot be queried again.
- **Proposed Description:** `"Create a Room: returns a one-time cleartext multi-use link_token (rm_...) and link_id. Copy link_token immediately; it is hashed at rest and never returned again by room_info. The owner auto-joins as first active member."`

#### `room_join`
- **Current Description:** `"Join a Room with a multi-use link, explicit consent (literal boolean true), and an actor credential. The link admits new identities up to the cap; it cannot overwrite an existing member identity."`
- **First-Attempt Verdict:** **PASS**.
- **Observation:** Explicit mention of `consent: true` is effective.

#### `room_info`
- **Current Description:** `"Member-only view of a Room: state, cap, member count, roster with presence, owner. The ROOM OWNER additionally sees the link control surface (link_id, the identifier room_revoke_link needs, and link_revoked, confirming whether a revocation landed); ordinary members never see it, and link_token is never returned."`
- **First-Attempt Verdict:** **PASS**.
- **Observation:** Clear and honest regarding owner vs member privilege separation and the non-exposure of `link_token`.

#### `room_send`
- **Current Description:** `"Address one agent, a named group, or the whole room with a payload, returning durable per-recipient receipts. Each receipt separates delivery status from recipient read_status; the sender may audit its own targeted message while other non-addressees receive a redacted envelope. Optional sender-set message_kind (lowercase [a-z0-9_-], max 32 chars) labels the message as a first-class, queryable column on the event row: post message_kind 'result' when you finish a unit of work, message_kind 'status' for liveness, then poll with message_kinds [\"result\"] to consume only other agents' conclusions."`
- **First-Attempt Verdict:** **FAIL (on broadcast)**.
- **Defect:** Omits `"*"` syntax for the whole room. Error message misdiagnoses `"broadcast"` as a missing member.
- **Proposed Description:** `"Send a message to target_spec ('*' for whole room, 'agent_id' for unicast, or 'group_name' for group). Returns durable receipts. Optional message_kind ('result', 'status', 'question') enables filtered polling."`
- **Proposed Error Enhancement:** In `_reject_unroutable_specs`: if `spec in ("broadcast", "all", "everyone")`, raise `RoomError("recipient_not_found", f"Recipient '{spec}' is not valid; use '*' to broadcast to the whole room.")`.

#### `room_poll`
- **Current Description:** `"Replay ordered Room events from a per-member cursor. At-least-once; consumers ack to advance their own cursor. Optional message_kinds list (at most 64 entries) filters returned events to those whose message_kind matches an entry (e.g. message_kinds [\"result\"] to consume only finished-work posts); when absent, everything is returned. next_seq and cursor_head are always reported against the FULL stream, so a filtering caller pages matching events with no gaps or repeats and can ack cursor_head safely."`
- **First-Attempt Verdict:** **PASS**.
- **Observation:** `message_kinds` filter semantics are described with high precision.

#### `room_ack`
- **Current Description:** `"Advance this member's cursor to seq (monotonic MAX). Events below the cursor are never re-delivered; durable receipt rows addressed to this member are marked read through seq."`
- **First-Attempt Verdict:** **PASS**.

#### `room_heartbeat`
- **Current Description:** `"Refresh a member's presence (last_seen)."`
- **First-Attempt Verdict:** **PASS**.

#### `room_groups`
- **Current Description:** `"Add members to / remove from / list a named group for group-addressable sends."`
- **First-Attempt Verdict:** **FAIL (on invalid action guess)**.
- **Defect:** `action` parameter lacks explicit enumeration in schema and description.
- **Proposed Description:** `"Manage named groups for target_spec sends. Set action to 'add', 'remove', or 'list'. Group names can be targeted in room_send."`

#### `room_receipts`
- **Current Description:** `"Member-only: query delivery status and durable recipient read_status for outbox entry ids."`
- **First-Attempt Verdict:** **PASS (with provenance ambiguity)**.
- **Proposed Description:** `"Member-only: query delivery and read status for outbox entry_ids returned by room_send."`

#### `room_revoke_link`
- **Current Description:** `"Owner-only: revoke a Room link so it can admit no one. An unknown, already-revoked, or wrong-room link_id is refused with link_not_found (byte-identical to a link that never existed, so no link-id oracle); a malformed link_id is refused with invalid_argument. Success is only reported when the link was actually revoked. The owner can rediscover link_id via room_info."`
- **First-Attempt Verdict:** **PASS**.
- **Observation:** Exemplary description: explicitly details error behavior, input validation, and how to rediscover `link_id`.

#### `room_remove_member` & `room_close` & `room_leave`
- **First-Attempt Verdict:** **PASS**. Clear owner/member authorization boundaries.

---

### 3.2 Task & Roster Coordination Tools

#### `create_task`, `claim_task`, `update_task`, `complete_task`, `verify_task`
- **First-Attempt Verdict:** **PASS**.
- **Key Invariants Well Documented:**
  - `claim_task` returns `fencing_token`.
  - `update_task` mandates `fencing_token`.
  - `complete_task` documents hard gate on `verify_task`.

#### `send_message`, `poll_messages`
- **First-Attempt Verdict:** **PASS**.

---

## 4. Summary of Recommended Copy & Error Fixes

All proposed improvements are strictly non-breaking copy and error message enhancements (zero signature or behavioral changes):

1. **`server.py::TOOLS` Descriptions:**
   - **`room_send`**: Add `'*'` broadcast syntax and target types.
   - **`rotate_agent_credential`**: Add initial bootstrap omission rule (`current_token=None`).
   - **`register_agent`**: Note initial `actor_token` return and renewal token requirement.
   - **`room_create`**: Add hash-at-rest warning regarding cleartext `link_token`.
   - **`room_groups`**: Enumerate `action: 'add' | 'remove' | 'list'`.
   - **`room_receipts`**: Clarify `entry_ids` origin.
2. **`rooms.py::_reject_unroutable_specs` Error Hint:**
   - When an unrouted target is `"broadcast"`, `"all"`, or `"everyone"`, return actionable remediation text:
     ```text
     Recipient(s) are not members of this room: broadcast. (Did you mean '*' to broadcast to the whole room?)
     ```
