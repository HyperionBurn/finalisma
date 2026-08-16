# YC application draft

This is a truthful draft for the current milestone. Replace every bracketed
field with measured evidence before submitting.

## Application timing note (checked 2026-07-28)

The official YC application page currently lists the Fall 2026 on-time deadline
as July 27 at 8pm PT and says late applications are still considered, without a
guaranteed response timeline. Submit as soon as the facts and demo are ready;
verify the live status at [YC Apply](https://www.ycombinator.com/apply).

## What does your company make?

Weft is the secure coordination layer for AI-native engineering teams. It
lets two agent hosts pair through a one-time link, transfer only the approved
task context, and produce an auditable, evidence-gated handoff.

## What is the first workflow?

Non-production incident mirrors and pull-request review. A team already using two
agent hosts can create a link when the first agent hits a wall, consent to a
bounded context transfer, and get an evidence-gated result without copying an entire
conversation or sharing provider credentials.

## What is working today?

- Dependency-free Python MCP server with durable SQLite state.
- One-use pairing links with preview-before-consent.
- Member-bound session credentials and replayable ordered events.
- Scoped tasks, leases, fencing tokens, artifact hashes, and evidence gates.
 - 1057 passing standard-library tests, including concurrency, restart, HTTP,
  tenancy isolation, roster routing, durable outbox, bridge adapters, SDK
  flows, room lifecycle, cloud storage, cross-team boundary cases, identity,
  and the stdio-to-hosted bridge (docs/STDIO_BRIDGE.md).
- A locked same-machine coordinator benchmark with a 72.221 ms reference artifact.
  The latest recorded local gate (2026-08-14; historical 929-test artifact)
  measured 72.433 ms weighted median / 95.985 ms p95. Quality, semantic-digest, smoke, and credential checks
  passed; weighted-median, routing-fanout, and session-relay timing guards were
  red while OpenCode was active. A previous 928-test run passed all timing
  guards at 65.473 ms / 70.630 ms.
- A static marketing site (Astro-built) and a dependency-free coordinator. The coordinator is Python stdlib-only, SQLite, no CDN. An interactive product simulation, quickstart, and
  reproducible 42-second MP4/WebM demo generated from a real local coordinator run.

## Who is using it?

The product is **pre-design-partner and actively recruiting**. There are zero
design-partner teams today, so there is no customer-reported workflow, no
customer median time-to-first-handoff, and no four-week repeat rate. We will
not state numbers we have not measured.

What we can state truthfully:

- **Local protocol measurement, not customer data.** In a clean temp workspace
  on one machine, the real stdio path from coordinator start to a verified,
  evidence-gated handoff completed in **0.166 s** (measured 2026-08-05,
  `docs/PAIRING_UX.md`). This is a single-machine local measurement, not a
  customer median, and it does not include the human time to read docs,
  paste commands, and approve a host's MCP prompt. Treat it as a lower bound on
  the protocol, not an adoption signal.
- **One real host validated.** The weft MCP server completed a full
  two-agent handoff through a genuine MCP host — opencode 1.18.13 — from its
  own config, with a captured transcript (`docs/INTEROP_VALIDATION_2026-08-05.md`).
  That validation also found and fixed a real interop defect (fencing tokens
  were outside JavaScript's safe-integer range, which broke verify/complete in
  JS-based hosts). This is engineering evidence, not customer traction.

The plan is to recruit a small cohort of teams already running two MCP-capable
agent hosts, run their first three handoffs as concierge onboarding, and
measure first-handoff time and second-weekly-handoff rates on real teams. Until
those are real, the truthful statement is **"we are recruiting design
partners,"** not "teams use Weft."

## Why now?

AI-native teams increasingly use more than one agent host. The useful work is
distributed across coding, research, review, and operations agents, but the
handoff remains manual. MCP makes host integration possible; Weft supplies
the missing identity, scope, replay, and evidence contract.

## What is the insight?

Agent collaboration is not primarily a chat problem. It is a distributed-systems
problem with a human consent boundary. Fencing tokens, leases, idempotency,
ordered replay, and evidence gates are product primitives when agents can act
on a shared workspace.

## What is the 30-day experiment?

Recruit 10 teams manually, run their first three handoffs as concierge
onboarding, and charge at least three teams for continued use. Measure the
first evidence-gated handoff and second-weekly-handoff rates. If teams pair once but
do not return, narrow or kill the wedge before building hosted scale.

## What is not built yet?

The current release is not a hosted multi-tenant service. Before untrusted
public traffic we need shared transactional storage, OAuth/OIDC audience
binding, distributed rate limits, durable delivery/retry, data retention and
deletion, load tests, and operational alerting.
