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
- [ ] Rooms: create, invite-by-link, N-agent join, presence, roster, leave, close.
- [ ] Addressing: unicast, group, broadcast — all with delivery receipts.
- [ ] Ordered replay with per-member cursors; reconnect loses and duplicates nothing.
- [ ] Governance preserved: consent, scopes, leases, fencing, evidence gates, audit.
- [ ] All four connection tiers proven with committed transcripts.

### Hosted service
- [ ] Multi-tenant isolation with a negative test proving tenant B cannot read tenant A.
- [ ] Shared transactional storage, migrations, and a documented backup/restore. (2026-08-05 Wave F decision: v1 runs **SQLite-WAL**, single instance — superseding the earlier "Postgres" wording for the v1 milestone; Postgres is a later scale decision behind the same `StorageBackend` ABC. See §1 and the annotation at the top of this file.)
- [ ] Real auth: email + OIDC, sessions, revocation, credential rotation.
- [ ] Multi-instance safe: distributed rate limits, no single-node assumptions.
- [ ] Durable delivery under crash; DLQ recovery proven by a kill-mid-flight test.

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

### Truthfulness (non-negotiable, this product is sold on evidence)
- [ ] Every number on the site and in docs measured against current code.
- [ ] No claim of customers, traction, or verified hosts without a committed artifact.
- [ ] `AGENTS.md` and `AGENT_HANDOVER.md` current with the code in the same commit.

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
