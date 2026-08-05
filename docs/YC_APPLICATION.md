# YC application draft

This is a truthful draft for the current milestone. Replace every bracketed
field with measured evidence before submitting.

## Application timing note (checked 2026-07-28)

The official YC application page currently lists the Fall 2026 on-time deadline
as July 27 at 8pm PT and says late applications are still considered, without a
guaranteed response timeline. Submit as soon as the facts and demo are ready;
verify the live status at [YC Apply](https://www.ycombinator.com/apply).

## What does your company make?

Finalisma is the secure coordination layer for AI-native engineering teams. It
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
- 226 passing standard-library tests, including concurrency, restart, HTTP,
  tenancy isolation, roster routing, durable outbox, bridge adapters, SDK
  flows, and cross-team boundary cases.
- A locked same-machine coordinator benchmark with a re-baselined (2026-08-05)
  weighted median of ~68.8 ms against the current extended harness, with
  matching semantic digests and a green regression gate.
- A no-build launch site, interactive product simulation, quickstart, and
  reproducible 42-second MP4/WebM demo generated from a real local coordinator run.

## Who is using it?

`[FILL: number of design-partner teams]` teams are using it for `[FILL:
workflow]`. Median time from pairing link to first evidence-gated handoff is `[FILL]`.
`[FILL]` workspaces completed a second handoff within four weeks.

Until those fields are real, say “we are recruiting design partners,” not
“teams use Finalisma.”

## Why now?

AI-native teams increasingly use more than one agent host. The useful work is
distributed across coding, research, review, and operations agents, but the
handoff remains manual. MCP makes host integration possible; Finalisma supplies
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
