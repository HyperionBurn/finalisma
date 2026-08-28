# Why We Lose - August 2026

**Status:** Adversarial strategy memo; the strongest case for a buyer or investor to say no.
**As-of date:** 2026-08-28. Web documentation was accessed on 2026-08-28.
**Scope:** Hosted Weft as shipped in this repository and at the public deployment. This is not a balanced product brief and it is not a list of mitigations.

## The verdict

The strongest honest argument against Weft is that it is an ephemeral group message service presented through MCP, without a proven must-have customer and without a moat that the agent hosts already in the workflow cannot copy. A buyer who already uses one framework can get handoffs, groups, parallel execution, and persistence inside that framework. A buyer who needs cross-vendor work can use A2A, a queue, GitHub, or an existing chat channel. Weft asks that buyer to add an account, an agent key, a bridge, a bearer link, and a new room protocol before proving that the cross-vendor case occurs often enough to pay for.

The security-sensitive buyer has a second reason to refuse: a valid room link is a reusable cross-tenant capability. A holder of the link still needs an authenticated Weft account or agent key, but the room owner does not approve each joining identity. There is no recipient binding in the link. That is a reasonable mechanism for a trusted experiment; it is a policy blocker for organizations that require identity-bound invitations, SSO, audit controls, or data-retention guarantees.

If Weft cannot demonstrate repeated, paid workflows involving independently controlled agent hosts, the rational decision is to stop building the coordination layer. The evidence needed to change that decision is specified at the end.

## The three objections that can kill Weft

| Rank | Objection | Why it is existential | Evidence today |
| --- | --- | --- | --- |
| 1 | **The blocked buyer is not identified.** | If cross-vendor, multi-party coordination is an occasional demo rather than a recurring job, a room is not a product category. Existing frameworks and hosted coding-agent products absorb the common case. | No customer interview, paid-commitment, or retention evidence is recorded in the repository's own prior wedge research. One current-day internal MCP-client check proves a setup path, not a market. [`AUTORESEARCH_INTEROP_WEDGE_2026-07-30.md`](../AUTORESEARCH_INTEROP_WEDGE_2026-07-30.md) and [`INTEROP_VALIDATION_2026-08-05.md`](../INTEROP_VALIDATION_2026-08-05.md). |
| 2 | **The authorization model fails the security review.** | A link can be copied outside the intended organization and used to join a room across tenant boundaries. The owner can revoke the link, but cannot make the existing link recipient-specific. Missing SSO/OIDC, compliance, retention, and SLA evidence compounds the refusal. | The room implementation explicitly resolves a room by link token without a caller-tenant filter and permits multi-use redemption. The credential contract explicitly does not promise SSO/OIDC, enterprise compliance, an SLA, or production-data readiness. [`rooms.py`](../../src/weft_cloud/rooms.py), [`AGENT_KEYS.md`](../AGENT_KEYS.md). |
| 3 | **The wedge is a feature and the protocol is copyable.** | MCP hosts already own client orchestration and context. OpenAI, Microsoft, LangGraph, and Claude Code document native multi-agent coordination; A2A standardizes cross-vendor task exchange. A vendor can add a room or broker while retaining distribution, identity, billing, and workflow state. | Weft's hosted surface is 14 room tools around a proprietary `weft.a2a/2.0` namespace. The repository explicitly says that namespace is not a ratified protocol and disclaims universal host compatibility. [`mcp.py`](../../src/weft_cloud/mcp.py), [`AGENT_KEYS.md`](../AGENT_KEYS.md), [`PROTOCOL_V2.md`](../PROTOCOL_V2.md). |

These are ordered by ability to end the company, not by ease of fixing. Better copy, more tools, and nicer onboarding do not cure an absent recurring job or an authorization policy that a buyer cannot approve.

## 1. Demand: who is blocked, exactly?

The only clearly blocked person is a coordinator who has all of the following at once:

1. Two or more agents are controlled by different vendors, runtimes, or organizations.
2. They must exchange live messages while a common owner needs one ordered view of the exchange.
3. The agents cannot be put in one framework or exposed as mutually reachable HTTP services.
4. Message ordering, per-recipient visibility, replay, and delivery acknowledgement are worth adding a new service and credentials.

That is a concrete situation. The repository does not show how often it happens, who owns the budget, or whether the same team has the problem twice. The prior wedge research says there are no customer interviews, paid commitments, or retention data. This is not a small evidence gap; it leaves the demand claim untested. [`AUTORESEARCH_INTEROP_WEDGE_2026-07-30.md`](../AUTORESEARCH_INTEROP_WEDGE_2026-07-30.md)

The common alternatives are already inside the buyer's existing control plane:

- OpenAI's Agents SDK documents agents-as-tools, handoffs, code orchestration, and parallel execution with `asyncio.gather`. Its sessions documentation lists persistent session backends. That covers a team that controls one application and one provider ecosystem. [OpenAI orchestration](https://openai.github.io/openai-agents-python/multi_agent/), [OpenAI handoffs](https://openai.github.io/openai-agents-python/handoffs/), [OpenAI sessions](https://openai.github.io/openai-agents-python/sessions/)
- Microsoft Agent Framework documents Sequential, Concurrent, Handoff, Group Chat, and Magentic workflow patterns, plus human approval and request-information steps. A Microsoft-stack buyer can keep workflow state and policy where the workflow already runs. [Microsoft workflow orchestrations](https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/)
- LangGraph documents a stateful runtime, durable execution, human-in-the-loop, and checkpointers/stores for thread and cross-thread state. A buyer using LangGraph does not need a second room service for the same application boundary. [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview), [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- Claude Code Agent Teams document shared tasks, direct teammate messaging, and a lead session that coordinates the team. The page also records known limitations, so this is not a claim that it replaces every Weft use; it is evidence that one major host is already shipping the user-facing collaboration primitive. [Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams)
- GitHub documents asynchronous Claude and Codex third-party coding agents that can be assigned issues or pull requests and return a reviewable change. For coding teams, the repository and pull request are an existing shared work surface with identity and audit history. [GitHub third-party coding agents](https://docs.github.com/en/copilot/concepts/agents/about-third-party-coding-agents)

A2A makes the cross-vendor objection weaker, even though it does not reproduce Weft's exact N-party room. Its official concepts define Agent Cards, HTTP authentication, stateful Tasks, Messages, Parts, Artifacts, polling, SSE, and push notifications. A framework vendor can delegate to another vendor's endpoint without adopting Weft; a vendor can also compose several A2A tasks into its own group workflow. [A2A core concepts](https://a2a-protocol.org/latest/topics/key-concepts/), [A2A specification home](https://a2a-protocol.org/latest/)

MCP itself is not a room protocol. Its architecture is a host with one or more client instances connected to servers; the host manages lifecycle, security, consent, and context. That still matters against Weft: the host is the component with the context and policy authority, so a host vendor can add a broker, room server, or equivalent context-sharing feature without giving up the workflow. [MCP architecture](https://modelcontextprotocol.io/specification/2025-06-18/architecture), [Anthropic MCP documentation](https://docs.anthropic.com/en/docs/mcp)

### What the current evidence does and does not prove

The public deployment was probed on 2026-08-28. `/`, `/app`, and `/app/connect` returned HTML 200; `/health` returned 200; `/readyz` returned 200; an invalid `/j/<token>` returned HTML 404; and an invalid `/invite/<token>` returned HTML 200 titled `Invalid invite`. Those observations prove that selected pages and health routes respond. They do not prove authenticated room creation, cross-tenant joining, capacity, delivery latency, backup recovery, or customer demand. No authenticated production join was run for this memo.

The current-day internal verification supplied for this strategy task says that the configuration generated by the connect page works against one real MCP client. That is useful activation evidence. It is still one host transcript, not a host matrix, customer reference, second-session retention signal, or willingness-to-pay signal. The repository's committed real-host evidence is also one OpenCode transcript, plus an independent stdio client; the historical interop report marks other host integrations unverified. [`INTEROP_VALIDATION_2026-08-05.md`](../INTEROP_VALIDATION_2026-08-05.md), [`INTEROP_VALIDATION_2026-08-15.md`](../INTEROP_VALIDATION_2026-08-15.md)

The adversarial conclusion is therefore simple: **we have demonstrated that a client can be configured, not that a buyer is blocked.**

## 2. Trust and safety: why a security-conscious buyer refuses

The bearer-link design creates a policy problem even when the cryptography and SQL tenancy checks behave correctly.

In the hosted implementation, `join_room` accepts a multi-use link and requires literal consent, but the room is resolved by `(room_id, token_hash)` without filtering by the caller's tenant. The implementation then uses the room's own tenant for the membership row. Its docstring calls the link a cross-tenant capability. A caller still needs a valid hosted session or agent key; the important point is that possession of the link plus any eligible Weft identity is enough, and the room owner does not approve the specific identity. [`rooms.py`](../../src/weft_cloud/rooms.py)

The same source says the link is **not consumed**. It remains usable up to the room cap until expiry or revocation. `room_remove_member` is not a ban: a removed member who still has a valid link can rejoin. The owner can revoke the link, but that is a broad corrective action, not recipient-bound authorization. [`rooms.py`](../../src/weft_cloud/rooms.py), [`mcp.py`](../../src/weft_cloud/mcp.py)

That makes a copied URL a governance incident, not merely a leaked secret. A link can be pasted into a ticket, prompt, chat, terminal history, or browser session. The owner may not know which outside account redeemed it. Addressee redaction limits what non-recipients see in a message envelope, but it does not make the room membership or data policy recipient-bound. The event log and receipts still create a shared record whose owner and retention policy must be explained to every participating organization.

The repository is unusually explicit about what is missing. The hosted key contract says it does not promise SSO/OIDC, enterprise compliance, an SLA, or production-data readiness. The cloud design describes a v1 SQLite-WAL storage plane, with a one-writer implementation and a future storage seam rather than demonstrated multi-node operation. No repository evidence establishes backup/restore drills, disaster recovery, data residency, customer-managed keys, SCIM, DLP, or an independent tamper-evident audit export. [`AGENT_KEYS.md`](../AGENT_KEYS.md), [`CLOUD_SPINE_DESIGN.md`](../CLOUD_SPINE_DESIGN.md), [`storage.py`](../../src/weft_cloud/storage.py)

This does not mean the code has a demonstrated cross-tenant authorization bypass. The 2026-08-15 audit reported no critical or high findings and no data/authz bypass; it did record a medium availability issue for an unbounded `message_kinds` list that can produce SQLite variable errors, and low-severity 500 paths for malformed non-string fields. Those are maturity and availability signals, not proof of data theft. [`SECURITY_AUDIT_2026-08-15.md`](../SECURITY_AUDIT_2026-08-15.md)

The buyer's likely decision remains no: a room link is useful for a trusted, short-lived experiment, but the organization cannot turn it into identity-bound collaboration merely by adding a policy document.

## 3. The wedge is a feature, and the protocol is copyable

Weft's differentiators are real implementation choices: monotonic sequence numbers, broadcast addressing, addressee redaction, receipts, a room cap, a link token, and a stdio-to-hosted MCP bridge. The question is not whether these exist. The question is whether they are assets that a host or framework vendor cannot reproduce.

The current hosted surface answers that question badly. `HOSTED_TOOLS` contains 14 room operations:

`room_create`, `room_list`, `room_join`, `room_send`, `room_receipts`, `room_poll`, `room_wait`, `room_info`, `room_ack`, `room_heartbeat`, `room_leave`, `room_remove_member`, `room_close`, and `room_event_log`.

These are valuable semantics, but they are a bounded API around a room event log. No external standard requires a host to call them. The `weft.a2a` name is a proprietary profile namespace; the service code says it is not a ratified protocol standard. The key contract also disclaims universal third-party MCP-host compatibility. A vendor can copy the semantics, implement an MCP server or native host feature, and keep the user's identity, billing, model context, and workflow history. [`mcp.py`](../../src/weft_cloud/mcp.py), [`service.py`](../../src/weft_cloud/service.py), [`AGENT_KEYS.md`](../AGENT_KEYS.md)

MCP's official architecture makes the copy path plausible: hosts already manage multiple client instances, security, consent, and context. Weft is asking the host to add one remote room server and ask the model to use 14 tools. If the room becomes important, the host can absorb those operations. If it does not, the host can omit them. Weft owns neither a mandatory wire standard nor a distribution channel.

A2A increases this pressure. A2A does not define Weft's shared N-party ordered ledger, so it is not an exact replacement. It does standardize enough surrounding work - discovery, authentication, task state, messages, artifacts, and streaming - that a buyer can choose a vendor-owned composition rather than a Weft room. The gap between "A2A task composition" and "Weft room" is an implementation decision, not a protected market boundary.

The honest moat test is not protocol vocabulary. It is whether the same external organizations repeatedly choose Weft after existing tools fail, and whether their history, policy, or network makes replacement costly. Weft currently has no demonstrated installed base, shared directory, customer-specific policy corpus, or ratified protocol position that creates that effect.

## 4. Product as shipped: friction and unknowns beyond the headline weaknesses

The following are observed in the current repository. Where the repository only fails to provide evidence, the status is marked **unverified**, not inferred as a fact.

| Surface | What is actually implemented | Why a skeptical buyer says no |
| --- | --- | --- |
| Activation | Hosted MCP is Streamable HTTP; common MCP hosts use stdio, so a Python bridge is required. The bridge forwards the hosted 14-tool surface and reads the bearer from an environment variable. [`STDIO_BRIDGE.md`](../STDIO_BRIDGE.md) | The buyer installs a package or checkout, sets an environment variable, and edits host configuration before the first message. One current-day real-client verification shows that this path works once; it does not show that support burden is low across hosts. |
| Identity recovery | Hosted `agk_` keys are returned raw once, have no expiry column, and must be replaced when lost. [`AGENT_KEYS.md`](../AGENT_KEYS.md) | A lost key is an operational replacement flow. A lost room link has no owner-facing recovery story in the current request's stated product reality. This is avoidable friction during the exact handoff event on which adoption depends. |
| Email | With no `WEFT_SMTP_HOST`, `build_mailer` selects `LocalOutboxMailer`, which writes an outbox row and does not send. [`mailer.py`](../../src/weft_cloud/identity/mailer.py) | Invites travel as manually copied links in the default setup. The product's identity plane and its room handoff plane are therefore not the same as an invite-delivery product. |
| Hosted workflow model | The hosted MCP list is room-only. The repository's own contract says hosted MCP does not expose the self-hosted task, roster, outbox, registration, pairing, or tenancy surface. [`AGENT_KEYS.md`](../AGENT_KEYS.md) | There is durable room/event history, but no hosted task/artifact/job record, completion lifecycle, or portable workflow object. A buyer must rebuild those meanings in messages. |
| Persistence | A room has an ordered event log and receipts, but the default room TTL is seven days. [`rooms.py`](../../src/weft_cloud/rooms.py) | This is persistence of a short-lived coordination channel, not a durable project memory. After the room's useful window, the buyer still owns the responsibility for extracting state. No hosted export or migration package was found in the current route/tool source search. |
| Quotas | Free plan: five rooms, 15 members per room, 10,000 events/month, and 60 messages/minute. Pro code limits are 50 rooms, 50 members, and 100,000 events/month; billing and a production plan contract are not evidenced here. [`quotas.py`](../../src/weft_cloud/quotas.py) | A five-room/15-member pilot cap is enough to test the idea, not enough to support a larger organization without an unverified commercial and capacity story. |
| Storage and operations | `SqliteWalBackend` documents one writer and many readers. The cloud design calls SQLite-WAL the v1 implementation and leaves Postgres as a future seam. [`storage.py`](../../src/weft_cloud/storage.py), [`CLOUD_SPINE_DESIGN.md`](../CLOUD_SPINE_DESIGN.md) | A buyer cannot infer horizontal capacity, failover, backup recovery, or a service-level commitment from a storage interface and WAL mode. Those operational properties are unverified. |
| Removal | Removing a member frees a seat but is not a ban; a member holding the valid room link can rejoin. [`rooms.py`](../../src/weft_cloud/rooms.py) | Incident response requires revoking the shared link, which affects every legitimate participant, or accepting a rejoin path. That is a poor fit for access reviews. |
| Interoperability proof | The bridge has local integration tests and the repository contains one committed real OpenCode transcript; other host integrations are marked unverified in the historical interop report. [`STDIO_BRIDGE.md`](../STDIO_BRIDGE.md), [`INTEROP_VALIDATION_2026-08-15.md`](../INTEROP_VALIDATION_2026-08-15.md) | A config that works for one client is not a compatibility contract. A buyer with a different host still bears integration and support risk. |
| Failure behavior | The security audit records a medium `message_kinds` availability path and low malformed-input 500 paths. [`SECURITY_AUDIT_2026-08-15.md`](../SECURITY_AUDIT_2026-08-15.md) | These are not catastrophic findings, but they show that a small input surface can still produce operational failures under unusual inputs. |

The product is therefore in an awkward middle: too short-lived and message-shaped to be a system of record, but too policy-sensitive to be treated as disposable chat infrastructure.

## 5. Switching and lock-in: low value or bad lock-in

There are two losing outcomes.

**If rooms do not accumulate valuable state, switching is easy.** A customer can stop using Weft and keep its framework, A2A service, GitHub issue, Slack channel, queue, or database. The room contains messages, not a durable task graph, artifact store, customer directory, or workflow history that creates a compounding advantage.

**If rooms do accumulate valuable state, switching is hostile.** Current source search found no hosted export route or export tool. A member can read the event log or poll messages, but there is no documented canonical export package that preserves room semantics, redaction decisions, receipts, cursors, identities, and replay behavior in another system. The buyer either writes an extractor or leaves history behind. That is lock-in without the benefits buyers expect from a system of record.

The protocol makes the adapter burden concrete. A replacement must understand `room_id`, link redemption, monotonic `seq`, addressee redaction, receipt/read state, idempotency keys, and the proprietary `weft.a2a/2.0` envelope. Existing workflows must also change their MCP configuration and bearer handling. Yet Weft does not own the surrounding model sessions or tasks. A framework vendor can preserve the valuable context and add the room later; Weft cannot assume that the customer's context will move to it.

The rational buyer asks: “Why should I migrate a working workflow to an expiring room broker that has no portable task record?” The current product has no answer that survives either the low-value or high-value switching case.

## 6. Substitution map: what the buyer can use instead

The table lists mechanisms, not brand impressions. A listed alternative is not claimed to match every Weft behavior.

| Substitute | What its primary documentation verifies | Why it can win against Weft | Where Weft is different, and why that may not matter |
| --- | --- | --- | --- |
| MCP host plus a room/broker server | MCP defines a host with multiple client instances; the host manages lifecycle, security, consent, and context. [MCP architecture](https://modelcontextprotocol.io/specification/2025-06-18/architecture) | The host keeps policy and context and can add or remove a room server without introducing a new account plane. | MCP alone has no ordered N-party room; the host must build or buy one. That is an opening only if hosts decline to build it. |
| A2A | Agent Cards, HTTP auth, Tasks, Messages, Parts, Artifacts, polling, SSE, and push are documented. [A2A core concepts](https://a2a-protocol.org/latest/topics/key-concepts/) | Cross-vendor delegation can use a standard discovery/task boundary and remain in the vendor's workflow. | A2A is not Weft's N-party shared log. The missing room profile is a product gap, not proof of buyer demand. |
| OpenAI Agents SDK | Agents-as-tools, handoffs, code orchestration, parallel `asyncio.gather`, and session backends are documented. [Multi-agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/), [sessions](https://openai.github.io/openai-agents-python/sessions/) | A team on one application can coordinate without a remote room, new link, or separate identity plane. | Cross-provider participants require an adapter or a different mechanism. The target segment must actually have that need. |
| Microsoft Agent Framework | Sequential, Concurrent, Handoff, Group Chat, Magentic, and human approval/request-information workflows are documented. [Workflow orchestrations](https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/) | Microsoft-stack teams retain workflow policy, state, and hosting in their existing environment. | It does not establish a public N-party room for arbitrary local clients. A buyer may still prefer the existing stack. |
| LangGraph | Long-running stateful agents, durable execution, HITL, persistence, checkpointers, and stores are documented. [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview), [persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | A graph owner gets durable thread state and recovery where the agents already run. | It is not a drop-in cross-organization room. Weft wins only when the graph boundary is the problem and the buyer accepts Weft's policy. |
| Claude Code Agent Teams | A lead can coordinate teammates through shared tasks and direct messages; the docs call the feature experimental and list limitations. [Agent Teams](https://code.claude.com/docs/en/agent-teams) | A Claude-only team can stay within Claude Code and avoid link distribution, hosted MCP credentials, and a second history store. | It is tied to Claude Code and is not a general cross-vendor service. That still covers a large class of development workflows. |
| GitHub third-party coding agents | GitHub supports assigning issues/PRs to Claude and Codex agents, asynchronous work, review, audit visibility, and policy controls. [Third-party coding agents](https://docs.github.com/en/copilot/concepts/agents/about-third-party-coding-agents) | The repository, issue, pull request, and audit trail are already the shared work surface for coding teams. | It is coding-specific and not live room messaging. It can still eliminate the reason to introduce a room for that buyer. |
| Slack, Git, queues, or a small service | The mechanism is familiar shared infrastructure; this repository's prior landscape describes channels, commits, queues, and their failure modes. [`LANDSCAPE_2026-08.md`](LANDSCAPE_2026-08.md) | Existing credentials, retention, search, and organizational policy often outweigh a new structured protocol. | They do not provide Weft's exact sequence/redaction/receipt contract. Buyers may accept that trade-off because the work already happens there. |

The burden of proof is on Weft to show that the last column is worth a new service. “MCP-compatible” is an activation property, not a distribution moat.

## 7. The experiment that could disprove this memo

Do not answer these objections with more protocol features. Run one observed, paid test against the demand claim.

### Fourteen-day cross-host test

Recruit eight qualified teams, each with a real incident-triage or review task that involves at least two independently controlled agent hosts. “Interested in multi-agent systems” is not qualification. A team qualifies only when it can name the two hosts, the human owner, the task that must cross the host boundary, and its current workaround.

For each team, observe one live run and record:

- host/vendor and version for every participant;
- whether the room link was created, redeemed, and used for a first handoff;
- whether a second handoff occurred without researcher prompting;
- whether ordering, redaction, and receipts changed the outcome compared with the existing workaround;
- the time from link creation to first useful exchange and the number of setup failures;
- whether the team repeats the workflow within 14 days;
- whether the team commits a $500 deposit for a production pilot, and whether two teams pay at least $1,000 total;
- the exact refusal reason when a team chooses its existing framework, A2A endpoint, GitHub, Slack, or a queue.

The precommitment is **6 of 8 teams complete a real cross-host workflow, 3 repeat it within 14 days, and 2 pay**. If the threshold fails, or if every successful team uses one vendor/framework family, the default decision is to kill the hosted room thesis rather than broaden the feature list. If a security review rejects bearer-link authorization before use, count that as a product-level disqualifier, not as a request to add an enterprise roadmap.

The evidence bundle should contain the task description, host/version record, redacted transcript, event/receipt proof, setup time, second-use timestamp, payment or written refusal, and a decision record. A landing-page signup, an unauthenticated live-page probe, or a single successful MCP transcript does not count as demand evidence.

## Limitations and open falsifiers

1. The memo has no customer interview transcripts, payment records, retention cohort, or production usage telemetry; the absence is itself the reason the demand objection ranks first.
2. The public probes were unauthenticated and do not establish production capacity, join success, latency, or tenant behavior.
3. The current-day one-client verification is internal evidence supplied for this task; it is not an independent customer reference.
4. A2A's official concepts do not define Weft's exact N-party ordered room. The claim here is substitution pressure around discovery, task exchange, and vendor-owned composition, not protocol equivalence.
5. MCP does not provide rooms by itself. The copyability argument is that the host already owns the policy/context boundary, not that MCP secretly contains a room.
6. The source search for export, SSO, OIDC, backup, and DR is evidence about this checkout, not proof that an uninspected deployment has no private feature or operational process.
7. The `pro` limits in `quotas.py` show code values, not a verified billing offer, contract, or capacity guarantee.
8. SQLite-WAL can be reliable for a single node. The objection is the absence of demonstrated failover and recovery evidence, not a claim that WAL is inherently unsafe.
9. The 2026-08-15 security audit is historical. Its findings should be rerun against the current revision before a security decision.
10. A buyer with a trusted, short-lived, cross-host workflow may rationally accept the bearer link. That buyer must repeat the workflow and pay for the thesis to survive.
11. Weft's ordering, redaction, and receipts may matter more than this memo assumes for some regulated or safety-critical workflows; no such customer evidence is currently recorded.
12. A host vendor may choose not to build a room even if it can. That is a possible opening, not a moat; the experiment must measure the vendor and buyer behavior.

## What would change my mind

I would change the verdict if named teams repeatedly used Weft with different host vendors because their existing framework, A2A endpoint, and shared work surface could not satisfy the same live task; if those teams accepted the bearer-link policy after documented review; if at least three repeated the task inside 14 days; and if at least two paid for continuation. A ratified room profile or host distribution partnership would further improve defensibility, but neither can substitute for repeated use.

Until that evidence exists, the strongest investment decision is **no**: Weft is a copyable room feature in search of a recurring cross-host job, with an authorization model that makes the most valuable buyers least able to adopt it.

## Sources and reproducibility

- Product source: [`src/weft_cloud/quotas.py`](../../src/weft_cloud/quotas.py), [`src/weft_cloud/rooms.py`](../../src/weft_cloud/rooms.py), [`src/weft_cloud/mcp.py`](../../src/weft_cloud/mcp.py), [`src/weft_cloud/storage.py`](../../src/weft_cloud/storage.py), [`src/weft_cloud/identity/mailer.py`](../../src/weft_cloud/identity/mailer.py).
- Product contracts: [`AGENT_KEYS.md`](../AGENT_KEYS.md), [`STDIO_BRIDGE.md`](../STDIO_BRIDGE.md), [`CLOUD_SPINE_DESIGN.md`](../CLOUD_SPINE_DESIGN.md), [`PROTOCOL_V2.md`](../PROTOCOL_V2.md), [`ADR-0001`](../adr/0001-link-as-universal-connector.md), [`ADR-0002`](../adr/0002-protocol-v2-envelope.md).
- Prior evidence: [`AUTORESEARCH_INTEROP_WEDGE_2026-07-30.md`](../AUTORESEARCH_INTEROP_WEDGE_2026-07-30.md), [`INTEROP_VALIDATION_2026-08-05.md`](../INTEROP_VALIDATION_2026-08-05.md), [`INTEROP_VALIDATION_2026-08-15.md`](../INTEROP_VALIDATION_2026-08-15.md), [`SECURITY_AUDIT_2026-08-15.md`](../SECURITY_AUDIT_2026-08-15.md).
- Live deployment probed on 2026-08-28: `https://weft.switzerlandnorth.cloudapp.azure.com/`, `/app`, `/app/connect`, `/j/rm_invalid_strategy_audit`, `/invite/fiv_invalid_strategy_audit`, `/health`, and `/readyz`.
- Official competitor/protocol documentation is linked inline above. No landing-page claims were used as evidence.
- Lightweight review command for the repository's interop report structure (not a build): `python -B scripts/weft_research_critic.py --report docs/strategy/LANDSCAPE_2026-08.md --matrix research/interop-matrix.json`.
