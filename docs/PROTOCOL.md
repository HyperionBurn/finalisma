# Weft Coordination Protocol Preview 0.1

Weft is a coordination protocol carried by MCP tools. MCP provides the
host-neutral discovery and call boundary; Weft defines the durable team
semantics behind those calls.

## Why these invariants matter

Agent handoffs fail in the gaps between messages: a stale worker keeps writing,
the same task is created twice, a reconnect loses the last event, or “done” is
only a prose claim. Weft makes those failure modes explicit with leases,
fencing tokens, idempotency, ordered replay, and evidence before completion.
They are the product contract, not implementation trivia.

## Invariants

1. Every mutation is attributable to a `team_id` and logical `agent_id`; when
   actor authentication is required, the caller must also prove that identity
   with its bound `actor_token`.
2. Every task claim is atomic and returns a random fencing token plus a lease.
3. A stale token cannot update, verify, or complete a task.
4. A task with an overlapping canonical workspace scope cannot be claimed by
   two active tasks at the same time.
5. Duplicate descriptions are compared against open work at a 0.55 similarity
   threshold; a duplicate result does not create a second task.
6. Message and task idempotency keys return the original operation result.
7. The server never executes commands found in task or message payloads.
8. Completion requires a passed evidence gate.

## Identity and credential boundary

Schema v3 adds one agent credential per `(team_id, agent_id)`. A new identity
receives its raw `actor_token` once from `register_agent`; a new
identity accepted through `join_pairing` receives one in the join
result. The host must keep that value in secret storage. Weft persists only
the SHA-256 hash in `agent_credentials`, never the plaintext token, and does not
return the token again when an existing identity registers or joins.

Existing identities and team/work-plane calls are protected in actor-auth
required mode. The matching `actor_token` is passed to tools such as
`create_pairing`, `create_task`, `team_status`,
`send_message`, `heartbeat`, and the task lifecycle tools.
An already registered invitee must also pass its token to
`join_pairing`; the one-time pair token cannot be used to overwrite an
existing identity. A genuinely new invited identity may bootstrap through the
pairing.

Actor tokens are not session tokens. Actor tokens authorize the durable logical
identity on the team/work plane. Pairing creates separate, member-bound session
tokens used only by the `session_*` tools. Neither credential can be
substituted for the other.

`rotate_agent_credential` accepts `team_id`, `agent_id`, and
`current_token`. It returns the replacement `actor_token` once and invalidates
the old token atomically. A v2 database migrates to schema v3 without fabricating
credentials for existing rows. Such an identity must be bootstrapped through a
trusted local operator call to the rotation tool without `current_token`, or
work must move to a newly paired `agent_id`; required mode does not permit
re-registration or re-pairing to claim an existing ID.

## Message envelope

`send_message` persists this shape:

```json
{
  "protocol": "weft.a2a",
  "version": "1.0",
  "message_id": "msg_...",
  "type": "task.progress",
  "task_id": "task_...",
  "sender": { "agent_id": "agent-a", "model": "gpt-5.6-luna" },
  "recipient": { "agent_id": "agent-b" },
  "timestamp": "2026-07-28T00:00:00.000Z",
  "correlation_id": "task_...",
  "payload": { "progress": 45, "note": "API contract mapped" },
  "capabilities": ["coding"],
  "trace_id": null,
  "signature": null,
  "priority": 2
}
```

`recipient: {"scope":"broadcast"}` is used for team announcements. Payloads
are application data, not server instructions. `signature` is reserved for a
future signed-message extension. Transport authentication and actor
authentication are separate: an HTTP bearer token protects the endpoint, while
the per-agent `actor_token` proves the logical sender for protected calls.

## Task lifecycle

```text
pending -> assigned -> in_progress -> review -> verified -> done
             |             |            |
             +-----------> blocked <----+
```

`create_task` performs duplicate detection and routing. A claimed
task receives a lease and fencing token. Heartbeats renew the lease. When a
lease expires, the server returns the task to `pending` and records an audit
event, allowing another agent to recover it safely.

## Quality gate

`verify_task` accepts a list of named checks with `passed`, `failed`,
or `unknown` status and a list of workspace-relative artifact paths. The server
then:

- resolves every path under the configured workspace, including symlinks;
- checks that artifacts exist and are below the size limit;
- confirms artifacts stay within the task's declared scope;
- records SHA-256 hashes and a redacted pass/fail summary;
- scans for high-confidence private-key and common provider-token signatures;
- optionally requires a different reviewer identity; and
- moves the task to `verified` only if every gate passes.

The server records the checks supplied by an agent; it does not execute an
arbitrary command supplied by a model. A supervising agent or host should run
tests/lint/build in its own approved execution surface and submit the result.

## Model slots

Model slots are data, not hard-coded provider branches:

| Slot | Intended lane | Route recorded |
| --- | --- | --- |
| `gpt-5.6-luna` | planning, critique, coding, research | native |
| `qwencloud/qwen3.8-max-preview` | coding, research, long context | OpenCodex/QwenCloud |
| `longcat/LongCat-2.0` | parallel execution, coding, knowledge work | OpenCodex |
| `opencode-go/mimo-v2.5` | security review, coding, reasoning | OpenCodex |

The selected slot is stored in the agent/task/message record. Weft does not
launch a provider, hold its key, or make a recorded slot available. A host or
native child-agent orchestrator must request the exact model ID. If that backend
rejects or cannot run the requested model, report that failure and stop that
lane; never silently substitute another model.

## MCP compatibility

The server exposes standard JSON-RPC MCP methods:

- `initialize`
- `tools/list`
- `tools/call`
- `ping`

Stdio uses one newline-delimited JSON-RPC message per line and writes logs only
to stderr. HTTP uses `POST /mcp`, validates optional `Mcp-Method` and `Mcp-Name`
headers when supplied, rejects invalid `Origin` values, and binds to localhost
unless the operator explicitly chooses otherwise with a bearer token.

The CLI's default `--actor-auth auto` policy trusts stdio and requires actor
credentials for HTTP, including loopback HTTP. Operators may require actor
credentials on stdio with `--actor-auth required`. Explicit trusted HTTP is
allowed only on `127.0.0.1`, `localhost`, or `::1`, emits a security warning,
and is rejected for non-loopback binds. This remains a single-process,
single-node SQLite coordinator, not a horizontally scalable or multi-tenant
authorization service.
