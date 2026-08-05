# Two-agent quickstart

This is the shortest interoperability test after both MCP hosts have loaded
the same `finalisma` server entry.

## Agent A

```text
agent_a = finalisma_register_agent(team_id="demo", agent_id="agent-a", name="Planner", role="architect", model="gpt-5.6-luna", capabilities=["planning", "research"])
# Persist agent_a.actor_token once in the host's secret storage.
finalisma_create_task(team_id="demo", created_by="agent-a", title="Map the API contract", description="Identify endpoints, risks, and tests", scope=["docs/api.md"], idempotency_key="demo-api-map-v1", actor_token="<agent-a actor token>")
```

## Agent B

```text
agent_b = finalisma_register_agent(team_id="demo", agent_id="agent-b", name="Builder", role="coding", model="qwencloud/qwen3.8-max-preview", capabilities=["coding", "testing"])
# Persist agent_b.actor_token once in the host's secret storage.
finalisma_read_inbox(team_id="demo", agent_id="agent-b", actor_token="<agent-b actor token>")
finalisma_team_status(team_id="demo", agent_id="agent-b", actor_token="<agent-b actor token>")
```

If the task was routed to B, claim it with the returned `task_id`, keep the
returned `fencing_token`, and send progress messages. If no active agent was
available at creation time, the task remains pending and can be discovered in
`finalisma_team_status`.

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
