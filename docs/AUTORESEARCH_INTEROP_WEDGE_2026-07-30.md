# Weft interoperability wedge autoresearch

As of: 2026-07-30 (Asia/Dubai)  
Research mode: primary-source protocol and host documentation, repository inspection, and adversarial rubric review  
Host matrix count: 9  
Evidence status totals: verified=0; documented-unverified=9; adapter-required=0; unsupported=0.  
Competing or adjacent systems reviewed: 10

## Executive verdict

Decision: EVIDENCE-BACKED CROSS-HOST INCIDENT-TRIAGE HANDOFF

Weft should launch as the **evidence-backed handoff layer for incident triage between two independently operated AI coding hosts**. It should not launch as a universal agent standard, an autonomous model router, or a hosted multi-tenant platform.

The strongest current product is narrower and more defensible than the broad pitch:

> When the first coding agent stalls during a production incident, hand a bounded investigation to a second agent in another host, preserve consent and scope, and return an artifact-linked evidence record in under 15 minutes.

This is a falsifiable wedge, not a confirmed market fact. The repository has a capable single-node coordinator, but it has **zero fresh real-host interoperability runs** in the evidence matrix and no customer-interview evidence. The next 14 days should therefore optimize for two things only: prove two real hosts can complete the workflow, and learn whether incident teams will repeat and pay for it.

The new perspective is decisive:

- MCP is the host insertion surface. It exposes Weft's coordinator operations to an existing agent; it does not itself create peer-to-peer autonomy.
- A2A is the closest standard model for independent agent discovery and task exchange. Weft does not currently implement the A2A 1.0 wire contract and must not imply conformance.
- ACP connects an agent runtime to an editor/client. AG-UI connects agents to user interfaces. Neither is an agent-to-agent substitute.
- Weft's useful IP is the opinionated trust and work-state layer: one-use pairing, recorded consent attestation, identity-bound credentials, leased tasks, fencing, ordered replay, idempotency, workspace-contained artifacts, and evidence records.
- The product should publish an **Interop Profile 0.1** over MCP, with an optional future A2A adapter. It should earn the word “standard” through independent implementations and conformance evidence, not declare it in advance.

## Evidence policy and hard boundaries

This report uses four host statuses:

- `verified`: a fresh, versioned, redacted, end-to-end host transcript and expected assertions exist in this repository.
- `documented-unverified`: current official documentation describes a compatible MCP path, but no fresh Weft host run exists here.
- `adapter-required`: the documented host surface cannot consume Weft directly and needs maintained translation code.
- `unsupported`: official evidence shows the required path is unavailable.

All nine current rows are `documented-unverified`. Documentation proves only that a host has an MCP client path. It does not prove that the host will preserve actor/session credentials, invoke wait or poll correctly, present consent to a person, honor cancellation, or complete the full evidence workflow.

The following semantic boundaries are non-negotiable:

- `finalisma.a2a/1.0` is currently a proprietary repository namespace, not proof of [A2A Protocol 1.0](https://a2a-protocol.org/latest/specification/) conformance.
- `consent=true` is a type-strict, stored caller attestation. It is not proof that a human saw and approved a host-native consent screen.
- Registered and offered capabilities are descriptive strings. They are not yet a negotiated, granted, and data-plane-enforced capability set.
- A `verified` Weft task currently proves workspace containment, artifact hashing, secret-pattern screening, and recorded check assertions. The coordinator does not independently execute every submitted check, and an unauthenticated reviewer name is not independent attestation.
- Audit rows are append-only through the application API, not tamper-evident against a machine owner who can edit the SQLite file.
- Model catalog entries are host-recorded route labels. Weft does not authenticate to or launch those providers.
- The earlier 95.31% benchmark improvement was a same-machine, before/after SQLite hot-path result measured against a 3-scenario harness. The gate was re-baselined on 2026-08-05 (~68.8ms weighted median) against the current extended harness; see docs/PERFORMANCE.md. It says nothing about model speed, network latency, horizontal scale, uptime, or demand.

## Exactly one launch wedge

### Selected wedge

**Evidence-backed cross-host incident-triage handoff for AI-native engineering teams.**

| Requirement | Falsifiable hypothesis |
|---|---|
| Buyer | VP or Head of Engineering at a 10–100-person AI-native software company. |
| Champion | On-call Staff Engineer, tech lead, or SRE who already uses at least two AI coding hosts. |
| Painful trigger | A regression or outage where agent A has partial findings but lacks context, permission, or confidence to finish safely. |
| Current workaround | Copy a symptom and file list into a second chat, coordinate in Slack or PR comments, recreate tool configuration, and manually decide whether the second answer is trustworthy. This is a hypothesis until interviews confirm it. |
| Product action | Agent A creates a scope-limited pairing; agent B previews the policy, records consent, claims the investigation, and returns an artifact-linked evidence record. |
| Activation | A real, non-toy task reaches its first evidence-backed handoff within 15 minutes of link creation. |
| Retention | The same workspace completes a second handoff without founder assistance within 14 days. |
| Pricing test | A $500, 30-day design-partner deposit credited toward $1,000 per workspace per month. The price buys coordination, auditability, and saved incident-decision time—not tokens. |
| Success threshold | At least 6 of 8 recruited teams run a real task; 5 activate; 3 repeat; 2 pay or sign an equivalent paid-pilot commitment. |
| Kill criterion | Fewer than 3 teams use the product beyond concierge setup, fewer than 2 repeat, or no team accepts the deposit. |

Why this wedge and no other one:

- Incident work has an accountable owner, a clock, and a reason to constrain context and demand evidence.
- The repository already contains task scope, leases, fencing, replay, credential separation, and an evidence gate. The wedge uses those primitives instead of hiding them behind generic “team” language.
- [Google's 2025 DORA research](https://cloud.google.com/blog/products/ai-machine-learning/announcing-the-2025-dora-report) reports 90% AI use among nearly 5,000 technology professionals while 30% report little or no trust in AI-generated code. That supports a broad trust problem, not demand for Weft or this exact workflow.
- Native products are rapidly absorbing generic multi-agent features. [Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams) provides shared tasks and direct teammate messaging inside Claude Code, while [GitHub third-party coding agents](https://docs.github.com/en/copilot/concepts/agents/about-third-party-coding-agents) lets users assign work to Claude or Codex on GitHub. Cross-host evidence and bounded handoff must be better than merely adding another agent.

Disconfirming evidence to seek deliberately:

1. One well-configured agent plus tools resolves incidents as well as two agents.
2. Native host review and GitHub comments provide enough context and auditability.
3. Access to logs, production systems, or approvals—not handoff integrity—is the actual bottleneck.
4. Responders do not trust agent-submitted evidence enough to change a mitigation decision.
5. Incidents are too infrequent for 14-day repetition.
6. Every qualified buyer requires hosted tenancy, SSO, and compliance controls before even a non-production trial.

If the frequency hypothesis fails, do not silently widen the pitch. End this experiment, retain the protocol findings, and run a separate goal for a higher-frequency risk-bearing workflow.

## What a real user gets

The user does not get faster models. They get a safer, less lossy transfer between tools they already use:

1. The first agent creates a bounded investigation instead of a prose dump.
2. The invited agent sees the declared scope and policy before joining.
3. Each agent receives distinct identity and session credentials; provider keys remain with the host.
4. The second agent claims work under a lease and fencing token, reducing duplicate writers.
5. Messages and progress are replayable after a disconnect through an ordered cursor.
6. Completion is blocked until an artifact and declared checks are recorded.
7. The incident owner can inspect who did what and what artifact digest was returned.

The user still has to install or configure the MCP server in each host. A pairing link is a secure invitation **after** installation; host security policies prevent it from being a magic installer. ChatGPT web additionally requires a remote endpoint and plan/admin eligibility. A host may also ask for tool approvals or refuse long-running calls.

## Protocol landscape: do not collapse the layers

| Layer | Current standard or protocol | What it actually covers | Weft decision |
|---|---|---|---|
| Host to tools/context | [MCP 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25) | A host/client connects to servers over JSON-RPC, negotiates capabilities, and uses tools, resources, prompts, and client features. | Use as the primary installation surface. Prove lifecycle and transport behavior with a real SDK and real hosts. |
| Independent agent to agent | [A2A 1.0](https://a2a-protocol.org/latest/specification/) | Agent Cards, messages, tasks, artifacts, streaming, push, cancellation, and standard security schemes for opaque agents. | Treat as a future adapter and semantic reference. Do not claim conformance today. |
| Agent to editor/client | [Agent Client Protocol](https://agentclientprotocol.com/) | A JSON-RPC boundary between coding agents and editing environments, including terminal, plans, tools, and permissions. | Adjacent. Use it where a client forwards MCP to external agents; it is not Weft's peer protocol. |
| Agent to user interface | [AG-UI](https://docs.ag-ui.com/) | Typed lifecycle, message, tool, state, and interruption events for agent-facing applications. | `adapter-required` for a future live dashboard. Current Weft events are not AG-UI events. |
| Durable shared work state | No single standard supplies Weft's entire policy | Identity, consent records, leases, fencing, ordered acknowledgements, evidence gates, and retention. | This is the proprietary coordination profile and primary differentiation. |

[MCP's transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) provides stdio and Streamable HTTP, optional SSE streams, session identifiers, explicit session termination, and resumability through SSE event IDs. It also states that a transport disconnect is not task cancellation. [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) defines OAuth-oriented HTTP authorization and discourages protocol-level authorization for stdio.

[A2A](https://a2a-protocol.org/latest/specification/) is closer to the user's broad vision: it targets independent, opaque agents across frameworks and vendors. It already specifies discovery, task state, artifacts, streaming and push updates, cancellation, context identifiers, and API-key, HTTP, OAuth, OpenID Connect, and mTLS security schemes. A proprietary protocol should therefore add a narrow operational advantage or become an A2A profile/extension rather than reimplement A2A under a confusing name.

## Weft Interop Profile 0.1

This is a proposed product contract, not a ratified public standard. The profile composes MCP for host access, adopts A2A-compatible concepts where practical, and keeps Weft-specific trust/work guarantees explicit.

| Contract area | Minimum behavior | Ownership and mapping | Current state |
|---|---|---|---|
| Discovery | Host completes MCP initialize and tools/list; Weft returns protocol/profile version. Future agent endpoint publishes an A2A Agent Card. | MCP now; A2A adapter later. | MCP surface implemented; no A2A Agent Card. |
| Identity | Every durable agent identity has a unique actor credential; remote deployments validate OAuth/OIDC audience and tenant ownership. | Weft-specific identity over MCP transport auth. | Has one-time actor tokens and rotation; lacks OAuth/OIDC resource-server validation. |
| Consent | Preview policy, require literal consent attestation, bind it to pairing ID, actor ID, policy digest, timestamp, and host surface. | Weft-specific. | Literal boolean and preview exist; no policy digest or proven host UI/human approval. |
| Capabilities | Advertise versioned capabilities, compute requested/offered/granted sets, and enforce granted operations. | A2A Agent Card concepts plus Weft policy. | Free-form metadata only; negotiation and enforcement missing. |
| Tasks | Create, claim, lease, heartbeat, reassign, review, evidence, complete, fail, and cancel with explicit state transitions. | A2A Task/Artifact semantics plus Weft leases and fencing. | Core lifecycle exists; explicit cancel and reassignment are missing. |
| Ordered events | Every session event has immutable event ID, sequence, origin, trace ID, schema version, and idempotency key. | Weft-specific event log; can map task/artifact updates to A2A. | Implemented for the single-node store. |
| Idempotency | Create/send operations replay the original result for the same scoped key; state mutations use compare-and-swap or fencing. | Weft-specific operational guarantee. | Implemented for task, message, and session event creation; claim conflicts rather than replaying. |
| Reconnect | Client persists last acknowledged sequence, resumes after it, deduplicates at-least-once delivery, and handles expired/closed sessions distinctly. | Weft profile; transport may also use MCP/A2A streaming. | Server-side poll/wait/replay exists; no host adapter state machine or retry policy. |
| Evidence | Record artifact digest, command/check provenance, authenticated submitter/reviewer, timestamps, and immutable outputs; distinguish recorded assertion from independently executed check. | Weft-specific. | Hash, containment, secret scan, and submitted checks exist; authenticated independent review and command provenance do not. |
| Cancellation | User or authorized agent requests task cancellation; workers observe a cancellation state; final event is durable; transport disconnect never implies cancel. | A2A CancelTask semantics plus Weft cooperative worker signal. | Status enum exists, but no cancel tool or cooperative signal. |
| Errors | Versioned registry separates validation, auth, conflict, retryable transport, rate limit, expired state, policy denial, and internal errors. | MCP/A2A error mapping plus Weft domain registry. | Structured errors exist; registry and retry guidance are incomplete. |

The first conformance fixture should use two independently launched processes and a real MCP client library. It must assert initialize ordering, protocol version headers, Accept behavior, tools/list, one successful tool call, malformed request handling, pair/join, credential separation, reconnect/replay, duplicate suppression, cancellation, evidence semantics, and provider-secret non-transit. Only then should host runs layer on top.

## Host compatibility matrix

The canonical machine-readable matrix is [`research/interop-matrix.json`](../research/interop-matrix.json). It contains 9 host surfaces and 0 verified integrations.

| Host | Official mechanism | Likely Weft path | Evidence status | Blocking caveat |
|---|---|---|---|---|
| OpenAI Codex local clients | [MCP docs](https://developers.openai.com/codex/mcp/) document stdio and Streamable HTTP plus shared local config. | Direct MCP hypothesis | documented-unverified | No real Codex pairing/replay/evidence transcript. |
| ChatGPT web custom apps | [Developer mode docs](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt-beta) document remote MCP apps with plan/admin gates. | Remote MCP hypothesis | documented-unverified | Full write support is beta and gated; agent mode does not use custom apps. |
| Claude Code | [MCP docs](https://code.claude.com/docs/en/mcp) document local/remote configuration and OAuth. | Direct MCP hypothesis | documented-unverified | Interactive and headless authentication differ. |
| VS Code with Copilot | [MCP server docs](https://code.visualstudio.com/docs/agent-customization/mcp-servers) document workspace/user config, local commands, and HTTP. | Direct MCP hypothesis | documented-unverified | MCP server sandboxing is unavailable on Windows. |
| Cursor Agent | [MCP docs](https://cursor.com/docs/mcp.md) document project/global config, stdio, SSE, HTTP, and OAuth. | Direct MCP hypothesis | documented-unverified | Tool behavior and credential persistence are untested. |
| Gemini CLI | [MCP docs](https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html) document discovery, confirmations, status, and remote/local servers. | Direct MCP hypothesis | documented-unverified | Headless remote OAuth needs special handling. |
| OpenCode v2 | [MCP docs](https://opencode.ai/v2/docs/mcp-servers) document stdio, Streamable HTTP, and OAuth. | Direct MCP hypothesis | documented-unverified | Version-specific schema must be pinned. |
| Zed Agent | [MCP docs](https://zed.dev/docs/ai/mcp) document local/remote servers and ACP forwarding. | Direct or forwarded MCP hypothesis | documented-unverified | Partial MCP feature coverage and two permission boundaries. |
| Cline | [MCP overview](https://docs.cline.bot/mcp/mcp-overview) documents extension/CLI MCP support. | Direct MCP hypothesis | documented-unverified | Extension and CLI configuration differ. |

The fastest credible proof pair is **Codex plus Claude Code over one loopback or trusted-network HTTP coordinator**, because both have current official configuration and OAuth documentation and are explicitly relevant to the target user. A second pair should use **VS Code plus Gemini CLI** to reduce vendor-specific inference. No matrix row becomes `verified` without a versioned transcript artifact.

## Competitor and alternative map

The relevant market is not empty. The differentiation must be cross-host deployment plus bounded, evidence-backed work—not “multiple agents.”

| # | System | Category | What it proves | Gap relative to the selected wedge |
|---:|---|---|---|---|
| 1 | [A2A Protocol 1.0](https://a2a-protocol.org/latest/specification/) | Open agent-to-agent protocol | Independent agents can expose discovery, tasks, artifacts, streaming, push, cancellation, and security schemes. | It is a specification and SDK ecosystem, not a zero-config bridge for existing MCP-only hosts or an opinionated evidence policy. |
| 2 | [Agent Client Protocol](https://agentclientprotocol.com/) | Open agent-client protocol | Agents can plug into editors independently of model/provider. | Client/editor interoperability, not shared cross-agent work state. |
| 3 | [AG-UI](https://docs.ag-ui.com/) | Open agent-UI protocol | Typed streaming UI events and interruption patterns are becoming standardized. | UI boundary, not agent-to-agent coordination. |
| 4 | [Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams) | Host-native multiplayer | Shared tasks, direct messages, and a lead/teammate model are already product features. | Experimental and Claude-host-bound; no cross-vendor MCP install path. |
| 5 | [GitHub third-party coding agents](https://docs.github.com/en/copilot/concepts/agents/about-third-party-coding-agents) | Hosted collaboration/control plane | Users can dispatch Claude and Codex asynchronously from issues, PRs, VS Code, and GitHub. | GitHub/PR workflow and supported partner set define the boundary. |
| 6 | [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/multi_agent/) | Application framework | Manager-as-tools, handoffs, code orchestration, and evaluator loops are first-class. | The developer builds one application/runtime; it is not a drop-in shared state layer for separately owned hosts. |
| 7 | [Microsoft AutoGen](https://microsoft.github.io/autogen/stable/index.html) | Multi-agent runtime/framework | Event-driven local and distributed agents, messages, runtimes, and MCP extensions are mature concepts. | Requires application integration and runtime ownership rather than host-level installation. |
| 8 | [LangGraph Supervisor](https://reference.langchain.com/python/langgraph-supervisor) | Workflow framework | Supervisor and handoff patterns are packaged for programmable graphs. | Code-first orchestration inside one application graph. |
| 9 | [CrewAI](https://docs.crewai.com/) | Workflow framework/control plane | Crews, tasks, processes, flows, persistence, guardrails, and human triggers cover broad orchestration. | Requires adopting CrewAI's application model; not a neutral bridge between existing agent products. |
| 10 | [Google ADK with A2A](https://developers.googleblog.com/build-cross-language-multi-agent-team-with-google-agent-development-kit-and-a2a/) | Framework plus open protocol | Cross-language remote agent teams can be built and deployed using a standard protocol. | Developer-built services and A2A implementations are prerequisites; it does not turn arbitrary MCP hosts into peers by itself. |

Weft's defensible thesis is therefore: **use the MCP surface users already have, add the trust/work-state semantics protocols and host-native teams do not jointly provide, and make one high-value handoff measurable.** If hosts or A2A products add equivalent cross-host state and evidence with simpler onboarding, this thesis is disproved.

## Repository gap map

Repository evidence anchors:

| File and symbol | Direct finding | Research implication |
|---|---|---|
| [`src/weft_mcp/core.py`](../src/weft_mcp/core.py) `PROTOCOL_NAME` / `PROTOCOL_VERSION` | The repository labels its proprietary envelope `finalisma.a2a/1.0`. | Rename or qualify the namespace until an A2A Agent Card, standard operations, error mapping, and interoperability tests exist. |
| [`src/weft_mcp/core.py`](../src/weft_mcp/core.py) `MODEL_SLOTS` / `model_catalog()` | Entries are route/provider/capability metadata returned by the coordinator. | Describe these as host-recorded route labels, not provider integrations or spawned models. |
| [`src/weft_mcp/core.py`](../src/weft_mcp/core.py) `join_pairing()` | The method requires the literal JSON boolean `true` and records the join. | Call this a consent attestation until a real host UI proves policy preview and human approval. |
| [`src/weft_mcp/core.py`](../src/weft_mcp/core.py) `create_pairing()` / `register_agent()` | Capability values are stored as free-form strings. | Add offered/requested/granted sets and enforcement before saying capability negotiation. |
| [`src/weft_mcp/core.py`](../src/weft_mcp/core.py) `verify_task()` | The store checks artifact containment/hash and records caller-supplied check statuses. | Use “evidence-backed”; require authenticated reviewer or executed-check provenance for independent verification. |
| [`src/weft_mcp/core.py`](../src/weft_mcp/core.py) `TASK_STATUSES` / `update_task()` | `cancelled` exists in the data model, but no explicit authorized cancel operation exists. | Cancellation is the clearest protocol-level P0 gap. |
| [`src/weft_mcp/core.py`](../src/weft_mcp/core.py) `session_send()` / `session_poll()` / `session_ack()` | The store provides idempotent ordered events, replay, and monotonic acknowledgement. | This is a genuine single-node differentiator to preserve in the profile. |
| [`src/weft_mcp/server.py`](../src/weft_mcp/server.py) `handle_json_rpc()` | Internal tests exercise JSON-RPC initialization, discovery, and tool calls. | Add a real MCP SDK lifecycle/transport conformance fixture; internal handler tests are not full host proof. |
| [`src/weft_mcp/server.py`](../src/weft_mcp/server.py) `WeftDispatcher._apply_team_scope()` | HTTP can hard-scope a coordinator process to one team. | Useful single-workspace defense, but not storage-enforced multi-tenant isolation. |
| [`src/weft_mcp/server.py`](../src/weft_mcp/server.py) `_Metrics` / `run_http()` | The server exports transport counters and can bind beyond loopback with a warning. | Add domain funnel metrics, structured logs, and external TLS before a paid remote trial. |
| [`scripts/weft_performance_gate.py`](../scripts/weft_performance_gate.py) routing scenario | The benchmark registers synthetic agent rows and runs coordinator operations on one machine. | Preserve the optimization result, but do not describe it as real-agent concurrency, host latency, reliability, or demand. |

### P0: required before the 14-day claim can pass

1. **Real host evidence.** There are no captured, versioned runs from two independent hosts. Add a redacted fixture directory per host pair with configuration, host version, timestamps, wire/transcript capture, expected assertions, environment boundary, and SHA-256 manifest. Existing internal handler and socket tests are necessary but insufficient.
2. **MCP lifecycle conformance.** Audit [`src/weft_mcp/server.py`](../src/weft_mcp/server.py), especially `handle_json_rpc()` and the HTTP handler, against [MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) and transport requirements. Add a real MCP SDK test for initialize ordering, protocol headers, Accept negotiation, notifications, malformed input, and errors.
3. **Explicit cancellation.** `TASK_STATUSES` includes cancellation, and `server.py` recognizes an MCP cancellation notification, but `WeftStore` has no `cancel_task` operation/tool. Add authorized task cancellation, durable cancellation events, worker observation, and race tests.
4. **Truthful evidence vocabulary.** Change public copy and protocol docs so “evidence-backed” is the default phrase. Reserve “independently verified” for an authenticated reviewer or coordinator-executed check with immutable command output.
5. **Consent and capability precision.** Store a policy digest and consent timestamp/actor/host. Define requested/offered/granted capability sets and enforce grants, or label them descriptive metadata.

### P1: required for a credible paid single-node design partner

1. Add task reassignment/unclaim with fencing and audit events in [`src/weft_mcp/core.py`](../src/weft_mcp/core.py).
2. Publish a versioned error registry and client retry/backoff guidance in [`docs/PRODUCTION_PROTOCOL.md`](PRODUCTION_PROTOCOL.md).
3. Add domain metrics for link creation, acceptance, claim, evidence submission, completion, cancellation, reconnect, and failure. The current `_Metrics` surface in [`src/weft_mcp/server.py`](../src/weft_mcp/server.py) is mostly transport-oriented.
4. Add structured JSON logs with redaction and stable trace/correlation fields; free-form stderr is inadequate for an incident product.
5. Add an external evidence adapter for CI output or a signed reviewer attestation. Never execute arbitrary agent-submitted shell text inside the coordinator.
6. Create version-pinned install guides for the first four hosts and a compatibility test manifest generated from the machine-readable matrix.

### P2: required before internet-facing multi-tenant SaaS

1. Replace the single-node SQLite storage boundary with shared transactional storage that enforces tenant ownership in the data layer.
2. Validate OAuth/OIDC access tokens as a resource server with issuer, audience, scope, expiry, revocation/key rotation, and tenant binding.
3. Add distributed rate limiting, transactional outbox/dead-letter handling, and idempotent consumers.
4. Add retention/deletion policy, backup/restore, disaster recovery, key management, incident response, and cross-tenant adversarial tests.
5. Add OpenTelemetry-compatible traces, dashboards, service-level objectives, alerts, and provider circuit breakers.
6. Publish and test an A2A adapter only if customers need non-MCP agents; do not rename proprietary JSON-RPC semantics as A2A compliance.
7. Add an AG-UI adapter only if a live operations dashboard becomes part of the paid workflow.

## Security and deployment truth

The current safe description is: **dependency-free, durable, single-node SQLite coordinator for local or trusted-network design partners**.

It is not a safe public multi-tenant service today because:

- SQLite WAL and in-process connection pools are a single-node boundary.
- `--team-id` is an application-layer process boundary, not storage-enforced tenant isolation.
- HTTP may use a shared bearer token plus actor tokens; it does not validate OAuth/OIDC audiences.
- TLS requires an external reverse proxy for non-loopback use.
- The rate limiter and bounded handler concurrency are process-local.
- There is no transactional outbox/dead-letter queue for distributed delivery.
- There is no proven backup/restore, account revocation, per-tenant retention/deletion, or cross-tenant load/partition test.
- Stdio is trusted by default under actor-auth `auto`, so local process/user compromise remains in the threat model.

For the design-partner experiment, run one workspace/team per process on loopback or a trusted private network, use unique transport and actor credentials, put non-loopback HTTP behind TLS, keep model/provider credentials in each host, use non-production incident mirrors, and record cleanup/retention. Stop the trial if a prospect requires public production data before the P2 controls exist.

## Fourteen-day launch experiment

| Day | Action | Required evidence |
|---:|---|---|
| 1 | Freeze Interop Profile 0.1 terms and evidence vocabulary. | Versioned profile, error/state table, no A2A or independent-verification overclaim. |
| 2 | Build MCP SDK conformance fixture. | Passing initialize, headers, tools, notification, malformed input, and error transcript. |
| 3 | Implement and test cancel task plus cooperative observation. | Unit/race tests and durable cancellation event. |
| 4 | Run Codex as host A against loopback HTTP. | Version, redacted config, initialize/tools call, transcript, artifact hashes. |
| 5 | Run Claude Code as host B and complete one cross-host pairing. | Distinct processes, distinct credentials, pair/join/send/replay trace. |
| 6 | Complete incident-like evidence workflow and force one disconnect/reconnect. | Scope, claim, fencing, replay, evidence semantics, cancel test, completion. |
| 7 | Repeat with VS Code and Gemini CLI. | Second vendor-diverse host pair; update matrix statuses only for proven surfaces. |
| 8 | Recruit eight qualified teams through direct outreach. | Two-host usage and on-call qualification logged; no invented pipeline counts. |
| 9 | Run two concierge non-production incident mirrors. | Funnel timestamps, confusion points, manual workaround baseline. |
| 10 | Run two more; remove onboarding friction only. | Time-to-first-evidence-backed-handoff and failure categories. |
| 11 | Ask activated teams to run a self-directed handoff. | Founder assistance level and replay/cancellation failures. |
| 12 | Ask for the $500 deposit before offering roadmap promises. | Payment, signed paid-pilot commitment, or explicit refusal reason. |
| 13 | Measure repetition and conduct loss interviews. | Second handoff count, why the evidence trail mattered or did not. |
| 14 | Apply the success/kill thresholds without narrative exceptions. | Written continue, narrow, or stop decision with raw funnel denominator. |

Instrumentation events:

- `link_created`
- `policy_previewed`
- `consent_attested`
- `join_succeeded`
- `task_claimed`
- `first_event_acknowledged`
- `reconnect_replayed`
- `cancel_requested`
- `evidence_recorded`
- `task_completed`
- `second_handoff_completed`
- `deposit_requested`
- `deposit_committed`
- `failure_reason`

The primary metric is median link-created to first evidence-backed handoff. The retention metric is workspaces with a second completed handoff within 14 days. The commercial metric is deposits or equivalent signed paid commitments divided by qualified teams that completed a real task.

Required launch artifact package:

- **YC evidence memo:** one page with the exact buyer/job, raw funnel denominators, deposits or refusal reasons, retention result, architecture diagram, single-node boundary, and links to redacted host evidence. No projected customer or revenue numbers.
- **Product Hunt package:** a truthful title/tagline, four-image gallery, 90-second narrated demo, first comment, compatibility status card, setup link, and a visible limitations statement. The demo must label any browser simulation and separately show the real two-host run.
- **Proof bundle:** redacted Codex/Claude Code and VS Code/Gemini CLI configurations and transcripts, host versions, timestamps, environment notes, expected assertion output, and a SHA-256 manifest.
- **Design-partner case note:** before/after workflow, elapsed time, founder assistance, what evidence changed the responder's decision, and permission to publish or an explicit internal-only label.
- **Decision record:** continue, narrow, or stop against the precommitted thresholds, including failed joins and non-paying teams rather than only successful demos.

## Claim ledger for launch copy

Safe now:

- “A single-node coordination layer for scoped, replayable, evidence-backed handoffs between MCP-capable agent hosts.”
- “Weft keeps model execution and provider credentials inside each agent host.”
- “The core has one-use pairing, actor/session credential separation, leased tasks, fencing, ordered replay, idempotency, and artifact hashing.”
- “Nine host surfaces have an official documented MCP path; none is yet marked verified in our compatibility matrix.”

Unsafe until new evidence exists:

- “A standard” or “A2A-compatible.”
- “Install with one link.”
- “Human-approved consent” without a tested host-native UI path.
- “Independently verified” without authenticated reviewer or executed-check provenance.
- “Production ready,” “enterprise secure,” “multi-tenant,” or “horizontally scalable.”
- “Works in every host,” “supports every model,” or any equivalent absolute compatibility claim.

## Limitations and open falsifiers

1. No real host was launched during this research run, so every host inference may fail on tool schemas, call duration, credential handling, approvals, or host policy.
2. Official documentation changes quickly; each verification artifact must include access date and host version.
3. The research contains no customer interviews, observed incidents, paid commitments, or retention data.
4. Incident triage may be too infrequent, too sensitive, or too access-constrained for this product stage.
5. The coordinator does not run models, so total outcome quality and latency remain host/provider dependent.
6. The current evidence gate cannot prove the truth of caller-submitted checks by itself.
7. MCP transport support is not equivalent to a reliable autonomous worker loop.
8. A2A, ACP, AG-UI, and host-native teams are evolving and may absorb parts of this product.
9. Public hosting is blocked on shared transactional storage, OAuth/OIDC, storage-level tenancy, distributed rate limiting, outbox/DLQ, retention/deletion, backup/restore, and operations.
10. This report evaluates the chosen engineering wedge. It does not establish demand for broader knowledge-work, sales, research, or consumer-agent use cases.

## Reproducibility

Run the machine critic from the repository root:

```powershell
python -B scripts/weft_research_critic.py --report docs/AUTORESEARCH_INTEROP_WEDGE_2026-07-30.md --matrix research/interop-matrix.json
```

The critic enforces all-or-nothing structural gates: host schema/count/status totals, dated official links, exactly one wedge, protocol contract dimensions, competitor count, repository gaps, deployment limitations, source density, and placeholder/overclaim rejection. Its passing output is necessary but not sufficient evidence; the independent professor-critic review still controls final research acceptance.
