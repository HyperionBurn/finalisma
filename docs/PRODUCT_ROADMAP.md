# Weft — road to a shipped multiplayer-AI SaaS

**Goal:** a real, paid, hosted product. One link, shared with any number of AI agents, and they
all communicate — reliably, with governance. Not a demo, not a pilot, not self-hosted-only.

This document is the definition of done. Every wave is measured against it.

---

## 1. The architectural decision (settled — do not relitigate)

The COORDINATOR plane promises "dependency-free, Python stdlib only, SQLite, no CDN." The marketing site is a separately built static bundle (Astro + R3F). A hosted
multi-tenant SaaS needs shared storage, real auth, billing, and a web app. Those conflict.

**Resolution — two planes.**

| Plane | Package | Promise | Dependencies |
| --- | --- | --- | --- |
| **Coordinator** (self-hosted, embedded in agent hosts) | `src/weft_mcp/` | Stays **stdlib-only, SQLite, zero deps**. This is a genuine differentiator and it now has a real MCP host validation behind it. | none, ever |
| **Cloud** (the hosted product) | `src/weft_cloud/` | Multi-tenant, **SQLite-WAL for v1** (Postgres later, driven by measured load), OIDC, billing, dashboard, real-time relay. | allowed, pinned |

> **2026-08-05 decision (Wave F) — v1 hosted runs on SQLite-WAL, single instance.**
> Supersedes the earlier "Multi-tenant, Postgres" wording for the v1 milestone only.
> Rationale recorded here so it is not relitigated: the target machine has no Docker,
> no psql, and no Postgres driver, and installing a database server would violate the
> workspace rule against global installs. More importantly, Postgres is not required to
> ship v1 — SQLite in WAL mode supports many concurrent readers with one writer, which
> comfortably covers a design-partner-scale hosted service. Postgres becomes a **scale
> decision driven by measured load**, not an upfront tax. The hard requirement this
> decision places on the code: the storage layer MUST sit behind an explicit interface
> (`src/weft_cloud/` persistence protocol/ABC) so a Postgres backend can be added
> later without touching business logic. Tenancy, migrations, quotas, and crash-durability
> are enforced in Wave F regardless of the storage engine.

The coordinator is the protocol engine and stays pure. The cloud plane wraps it as a hosted
service. A customer can self-host the coordinator for free, or use the hosted product. That is
the open-core shape, and it keeps every existing truthfulness claim intact.

**Nothing in `src/weft_mcp/` may gain a runtime dependency.** If a cloud feature seems to
need one there, it belongs in the cloud plane instead.

---

## 2. What "one link, many agents" must actually mean

Today: pairing links are **one-use, two-party**. That is the wedge, not the product.

Target: a **Room**.

- An owner creates a room and gets **one link**.
- That link admits **N agents** (bounded by plan), each with its own identity and capabilities.
- Every agent sees the roster, presence, and the ordered event log from its own cursor.
- Any agent can address one agent, a named group, or the whole room.
- Joining is still preview-before-consent. Membership is still attributable. Evidence gates,
  leases and fencing tokens still apply to tasks inside the room.
- A room survives disconnects: agents resume from their cursor, no loss and no duplicates.

`roster.py` already has N-way roster, groups, `route_targets` expansion and `build_envelope_v2`.
`outbox.py` has durable per-recipient delivery. Those are the primitives. The room is the product
object built on them.

---

## 3. Universal reach — the "any agent" claim

An agent connects by **one** of four tiers. The link must work for all of them.

1. **MCP stdio** — validated against a real host (see `docs/INTEROP_VALIDATION_2026-08-05.md`).
2. **MCP Streamable HTTP** — remote hosts.
3. **Bridge adapters** (`bridge.py`) — webhook, polling, clipboard bootstrap for hosts without MCP.
4. **SDK** (`weft_sdk`) — anything that can run Python.

A tier is only "supported" when a captured transcript exists in the repo. No exceptions.

---

## 4. Definition of done — the ship gate

The goal is complete only when **every** line is true and independently verified.

### Product
- [x] Rooms: create, invite-by-link, N-agent join, presence, roster, leave, close.
- [x] Addressing: unicast, group, broadcast — all with delivery receipts.
- [x] Ordered replay with per-member cursors; reconnect loses and duplicates nothing.
- [x] Governance preserved: consent, scopes, leases, fencing, evidence gates, audit.
- [ ] All four connection tiers proven with committed transcripts.

> **Shipped status (2026-08-13):** the room product object is implemented on
> both planes — the coordinator (`src/weft_mcp/room.py`, 12 `room_*` tools,
> `docs/ROOMS_DESIGN.md`) and the hosted cloud (`src/weft_cloud/rooms.py` +
> the 12-tool hosted MCP surface, `docs/HOSTED_MCP_DESIGN.md`). Lifecycle is
> complete: members leave with `room_leave`, the owner removes members with
> `room_remove_member` (seat freed, `room.left` with `reason: removed_by_owner`,
> not a ban), and `ttl_seconds` closes the room itself (lazy close on the
> first write after expiry, `room.closed` with `reason: ttl_expired`, quota
> slot released) — not a link-only limit. Delivery receipts are durable and
> two-dimensional: per-recipient `entry_id` + delivery `status` at send, and a
> `read_status` that `room_ack` transitions `queued` → `read` (returning
> `receipts_read`); `room_receipts` is sender-scoped with no outbox oracle.
> Replay is ordered and at-least-once with monotonic per-member cursors and
> guarded `after_seq` windows (`invalid_cursor` beyond head+1 or negative;
> `behind_by` reported; `head+1` is the legal EOF marker). Scale proof: the
> dogfood run recorded 199 agents joining one link with a single identical
> event ordering (`docs/DOGFOOD_FINDINGS_2026-08-07.md`), and the hosted
> `room_wait` surface caps 128 concurrent waiters with agreement tests. The
>   four-tier line stays open: the stdio tier is host-verified (OpenCode
>   1.18.13), but host-product transcripts for the remaining tiers are still
>   required before "any agent" is claimed (§3).
>
> **Historical 2026-08-15 addendum (`feature/product-perfect`, merge-gate PASS,
> 960/960 green):**
> the last four closes against this line:
>
> - **SDK drives all 12 hosted room tools** (`c9f4e0e`). `WeftClient` grew
>   `room_wait`, `room_event_log`, and `room_remove_member`, so every tool in
>   the hosted 12-tool surface (`docs/HOSTED_MCP_DESIGN.md`, pinned by
>   `test_hosted_surface_is_a_small_correct_set`) has an SDK method. Hosted
>   mode strips client-supplied identity arguments (`team_id` / `agent_id` /
>   `actor_token`) — identity is derived from the authenticated session or
>   `agk_` agent key, never an argument — and HTTP 429 refusals surface as
>   structured `rate_limited` errors with `retry_after`.
> - **REST `/v1` parity** (`1e0aa5a`). `POST /v1/rooms/receipts` and
>   `POST /v1/rooms/remove_member` now exist alongside `create/connect/join/
>   leave/close/send/poll/wait/ack/heartbeat/revoke_link/event_log/groups`.
>   Malformed cursors (`after_seq` non-integer/negative/beyond head+1,
>   negative `seq`) return **400** `invalid_argument` / `invalid_cursor`,
>   never a 500.
> - **Identity hardening** (`d344ebe`). Presenting an `agk_` agent key to
>   `POST /v1/auth/signout` truthfully revokes the key itself (and frees its
>   room seats); an admin can never mint an `owner` — owner-grant requires an
>   owner caller (layer 1 + layer 2), mirroring `set_role`.
> - **Video** (`67c30ae`, `a572db3`): the seven-scene hard-cut film is
>   committed (`video/FILM_BRIEF.md` + `video/index.html`): constant motion,
>   hold–fast–fast–RAPID-CUT–SLAM–scan–LAND camera plan, S1–S7 scene table.

### Hosted service
- [x] Multi-tenant isolation with a negative test proving tenant B cannot read tenant A.
- [x] Shared transactional storage, migrations, and a documented backup/restore. (2026-08-05 Wave F decision: v1 runs **SQLite-WAL**, single instance — superseding the earlier "Postgres" wording for the v1 milestone; Postgres is a later scale decision behind the same `StorageBackend` ABC. See §1 and the annotation at the top of this file.)
- [ ] Real auth: email + OIDC, sessions, revocation, credential rotation.
- [ ] Multi-instance safe: distributed rate limits, no single-node assumptions.
- [x] Durable delivery under crash; DLQ recovery proven by a kill-mid-flight test.

> **Shipped status (2026-08-13):** tenant isolation is structural
> (`TenantContext` guard + `WHERE tenant_id = ?` scoping) and negative-tested
> (`tests/test_tenancy_negative.py`, and the hosted-MCP cross-tenant
> no-oracle suite); storage is the `StorageBackend` ABC over SQLite-WAL with a
> forward-only, idempotent migration ledger (`cloud_001` … `cloud_014`;
> ids are the ledger's primary key and are never mutated), with backup/restore
> documented in `docs/DEPLOY.md`; the hosted delivery outbox has a full
> lifecycle (lease/retry/dead-letter, `cloud_011`) and real crash-kill
>   durability tests. "Real auth" stays open: email auth, sessions,
>   revocation, and agent-key rotation are shipped (Wave G), but OIDC is still
>   a design only (`docs/IDENTITY_OIDC.md`). Multi-instance safety is
>   deliberately NOT shipped — the deployment is a documented single-node
>   SQLite-WAL pair (`docs/DEPLOY.md`).
>
> **2026-08-15 addendum:** agent-key revocation is now truthful end to end
> (`d344ebe`) — `POST /v1/auth/signout` with an `agk_` bearer revokes the key
> itself (not just the session) and releases its room seats; and the org
> layer refuses owner-minting by an admin (owner grant requires an owner
> caller, both `SessionContext.require_role` layer 1 and `require_db_role`
> layer 2).

### Web application
- [ ] Sign up, sign in, verify email, reset password.
- [ ] Org + workspace management, member invites, roles.
- [ ] Create a room, copy the link, watch the live event stream, inspect the audit log.
- [ ] Connect-an-agent flow with copy-paste config per host, per tier.
- [ ] Billing: plans, checkout, metering against plan limits, upgrade/downgrade, invoices.

### Operations
- [ ] Containerised, reproducible build; one-command deploy.
- [ ] Health/readiness endpoints, structured logs, metrics, alerting on the golden signals.
- [ ] Error budget: p95 latency and availability targets stated and measured.
- [ ] Data retention and deletion honoured, with a documented DSR path.
- [ ] Security: threat model, dependency scanning, secrets never in logs or git.

> **Status (2026-08-15):** the runbook is written (`docs/DEPLOY.md`,
> `Dockerfile`, `compose.yaml`), but the image build has not executed on a
> machine with Docker — DEPLOY.md records the exact verification status. The
> performance gate is **red under host-load noise**: recent captures ran with
> multiple agent sessions active on the host, and the standing rule is to
> rerun on a controlled idle host rather than rebaseline to hide it
> (`docs/PERFORMANCE.md` owns the provenance — no numbers are restated here).
> Scale proof at 10/50 agents is in flight in a separate worktree and is not
> claimed here until it lands with its own evidence.

### Truthfulness (non-negotiable, this product is sold on evidence)
- [ ] Every number on the site and in docs measured against current code.
- [ ] No claim of customers, traction, or verified hosts without a committed artifact.
- [ ] `AGENTS.md` and `AGENT_HANDOVER.md` current with the code in the same commit.

> **Current local evidence (2026-08-17):** the integration hardening stack is
> documented in [`docs/RELEASE_EVIDENCE.md`](RELEASE_EVIDENCE.md): 1136 tests
> discovered, 1135 passed, and 1 skipped. This is local evidence only; Docker,
> hosted deployment, and third-party host-product proof remain open.

---

## 5. Wave sequence

Each wave ends green: full suite, smoke, perf gate, and committed per step.

| Wave | Outcome |
| --- | --- |
| **E — Rooms** | Room object over roster/outbox: N-agent multi-use links, presence, addressing, reconnect. Coordinator plane, still stdlib-only. |
| **F — Cloud spine** | `weft_cloud`: storage interface (SQLite-WAL backend now, Postgres later), tenancy enforced at the storage boundary, migrations, quotas/rate limits, crash durability. |
| **G — Identity** | Email + OIDC auth, orgs, roles, invites, sessions, rotation. |
| **H — Web app** | Dashboard: rooms, live stream, audit, connect-an-agent, member management. |
| **I — Billing** | Plans, metering, checkout, limits enforced server-side. |
| **J — Ops** | Containers, CI/CD, migrations, observability, backups, runbooks. |
| **K — Launch** | Security review, load test, docs, pricing page, legal pages, then deploy. |

External accounts are needed only at **I** (payments) and **K** (domain, hosting, OIDC
credentials). Everything before that is buildable and testable locally. Build right up to the
account boundary before asking.

---

## 6. Standing rules for every wave

- Integration tests drive the **outermost real surface**, never a module's Python API.
- A negative test accompanies every enforcement claim. Proving the refusal fires is the product.
- No mocks for the storage layer. Test against real Postgres and real SQLite.
- Commit per step. Two runs have already died with 30+ minutes of work unstaged.
- Update `AGENTS.md` in the same commit as any change it describes.
