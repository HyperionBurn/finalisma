# Weft YC readiness review

## Investor verdict

The infrastructure is becoming credible, but the sentence “connect two agents
and get a multiplayer team” is still a platform feature, not a company. A YC
partner would ask for one painful repeated workflow, a fast activation event,
and evidence that teams come back without founder-led prompting.

Weft should lead with:

> **The evidence-backed handoff layer for AI-native engineering teams.**
>
> Pair a coding agent with a research/review agent from a link, transfer only
> the approved task context, and produce an auditable, evidence-gated handoff.

This keeps the platform extensible while giving the first user a concrete job:
production incident triage, pull-request review, migration research, or a
blocked implementation that needs a second agent.

## Current diligence proof

The repository currently demonstrates 1288 passing standard-library tests
(1289 discovered, 1 skipped; measured locally on 2026-08-22),
including concurrent one-use pairing, member-bound session credentials,
ordered replay, restart durability, cross-team boundaries, workspace scope
checks, secret detection, HTTP origin/rate-limit behavior, tenancy isolation,
roster routing, durable outbox, bridge adapters, SDK flows, MCP handshake,
and the stdio-to-hosted bridge (docs/STDIO_BRIDGE.md).
A real launcher-level socket smoke test also passed health,
MCP initialization, metrics, CORS preflight, and coordinator team scoping. The
repository also contains a static marketing site (Astro-built from `web/`) and a dependency-free coordinator. A deterministic browser demo,
real protocol smoke runner, three focused articles, and launch drafts.

A locked same-machine performance gate also exercises 32-agent routing,
session relay, and full evidence-gated handoff scenarios. Its current reference
artifact is 72.221 ms; the latest complete local gate measured 137.404 ms weighted
median / 155.381 ms p95 and failed its timing guards while its quality sub-gates
passed. A controlled idle-host rerun remains required before attributing that
regression to code. The earlier ~77.5 ms and 95.31% figures are historical
provenance preserved in docs/PERFORMANCE.md and the baseline `history` array.
This is strong single-node engineering evidence, not a hosted-service latency claim.

That is credible technical proof for a single-node prototype. It is not proof
of provider uptime, multi-region reliability, customer demand, or paid
retention. The model catalog records the exact routes gpt-5.6-luna,
qwencloud/qwen3.8-max-preview, longcat/LongCat-2.0, and
opencode-go/mimo-v2.5 explicitly; execution and credentials remain owned by
the host rather than hidden inside the coordinator.

## First customer

Start with 10-100 person AI-native software teams already using two or more
agent hosts (Codex/OpenCode/Claude Code/Copilot-style workflows). They already
feel the pain of copying prompts, MCP configuration, credentials, and progress
between agents. Do not start with “every AI user” or with large enterprise
procurement.

## Activation and retention

The activation event is not “installed the MCP.” It is:

1. Agent A creates a link in under 30 seconds.
2. Agent B previews and consents in under 60 seconds.
3. Both agents exchange a task handoff.
4. The handoff reaches `verified` with an artifact/check record.

Measure this as **time-to-first-verified-handoff**. The week-4 retention loop is
the team's next incident/PR/task automatically reusing the same secure workspace
and policy rather than copying context again.

## Distribution loop

Every completed handoff can produce a safe invite link for a reviewer, another
specialist, or a customer-side agent. Start with founder-led distribution in
developer communities, GitHub PR comments, and direct design partners. The
product should make an invited agent's first join feel like a useful result,
not a signup funnel.

## Metrics to instrument before fundraising claims

- link-created -> link-accepted conversion;
- median and p95 time-to-first-verified-handoff;
- weekly retained workspaces with at least one evidence-gated handoff;
- sessions and evidence-gated handoffs per active workspace;
- invite-to-activated-workspace conversion;
- provider/error cost per evidence-gated handoff;
- task failure, replay, and reconnect rates;
- paid conversion and gross margin by workspace.

## Pricing hypothesis

Keep the first experiment simple:

- free: up to 15 members per room, 60 messages/min per room, 20 signups per IP
  per 15 minutes;
- pro: $39/seat/month, up to 50 members per room, same 60 messages/min per
  room;
- business: SSO, retention controls, shared storage, and deployment support.

Do not price by raw model tokens until the product owns a measurable outcome.
The initial paid value is safe coordination and saved engineering time.

## What must be true before saying “production”

The current implementation is a strong single-node technical foundation, not
yet a hosted multi-tenant service. Before untrusted public traffic or a serious
fundraising demo, prove shared storage migration, OAuth/OIDC audience binding,
distributed rate limiting, durable outbox/DLQ recovery, tenant isolation, data
retention/deletion, provider circuit breakers, and an operational dashboard.

## 30-day founder experiment

1. Recruit 10 engineering teams manually; do not wait for a polished marketplace.
2. Run their first three handoffs as concierge onboarding.
3. Record every failed join, unclear consent, lost event, and unsupported host.
4. Charge at least three teams before expanding the protocol surface.
5. Kill or narrow the wedge if teams pair once but do not return for a second
   weekly handoff.

This is the bar that converts a technically interesting coordination layer into
evidence for a fundable startup.
