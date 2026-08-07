# Two-agent quickstart

This is the shortest interoperability test after both MCP hosts have loaded
the same `finalisma` server entry. Every command below was verified in a timed
run on 2026-08-05 — first verified handoff completed in 0.17s wall-clock.

## Agent A

```text
register_agent(team_id="demo", agent_id="agent-a", name="Planner", role="architect", model="gpt-5.6-luna", capabilities=["planning", "research"])
```

Persist the returned `actor_token` once in the host's secret storage. Then:

```text
create_task(team_id="demo", created_by="agent-a", title="Map the API contract", description="Identify endpoints, risks, and tests", scope=["docs/api.md"], preferred_agent="agent-b", idempotency_key="demo-api-map-v1", actor_token="<agent-a actor token>")
```

## Agent B

```text
register_agent(team_id="demo", agent_id="agent-b", name="Builder", role="coding", model="qwencloud/qwen3.8-max-preview", capabilities=["coding", "testing"])
```

Persist Agent B's `actor_token` separately. Then claim and complete:

```text
claim_task(team_id="demo", agent_id="agent-b", task_id="<task_id from Agent A's task>", actor_token="<agent-b actor token>")
```

Keep the returned `fencing_token`. Write your artifact inside the declared scope,
then:

```text
verify_task(team_id="demo", agent_id="agent-b", task_id="<task_id>", fencing_token="<fencing_token>", artifact_paths=["docs/api.md"], checks=[{"name": "contract-review", "status": "passed", "evidence": "endpoints mapped, 3 risks identified"}], actor_token="<agent-b actor token>")

complete_task(team_id="demo", agent_id="agent-b", task_id="<task_id>", fencing_token="<fencing_token>", summary="API contract mapped with 3 findings", actor_token="<agent-b actor token>")
```

If no active agent was available at creation time, the task remains pending and
can be discovered in `team_status`.

Stdio defaults to trusted mode, but retaining and passing the actor tokens makes
the identity boundary explicit and is required by default over HTTP. The HTTP
transport bearer token is separate and does not identify Agent A or Agent B.

## Model note

The catalog records the exact routes gpt-5.6-luna,
qwencloud/qwen3.8-max-preview, longcat/LongCat-2.0, and
opencode-go/mimo-v2.5. A slot is data, not an API credential, and does not
make a provider available by itself. Configure the actual provider in the agent
host using its supported MCP/model settings; never commit a key into this
project. When a native child-agent lane is requested, use the exact configured
model ID. If the orchestration backend rejects it, report that failure; never
silently substitute another model.
