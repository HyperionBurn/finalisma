# Finalisma MCP

## The evidence-backed handoff layer for AI-native engineering teams.

Finalisma is a small, portable MCP server that gives two or more MCP-capable
agents a shared coordination layer for evidence-gated handoffs. When two
compatible hosts load the same MCP entry, they can share one task board, inbox,
lease system, and evidence trail without sharing conversation history or provider
credentials. The current release candidate proves that coordinator path locally;
live host-pair validation is still open and tracked separately from documented
MCP compatibility.

The core is intentionally dependency-free: Python standard library only, SQLite
for durable state, and no global install, PATH edit, service, registry change,
or API key in the repository.

## What it provides

- One versioned `finalisma.a2a/1.0` message envelope with sender, recipient,
  task, correlation, priority, capabilities, trace, and idempotency fields.
- Duplicate-aware task creation with capability-first routing.
- Atomic task claims, workspace-contained file scopes, leases, heartbeats, and
  fencing tokens so stale agents cannot keep writing after lease loss.
- Direct and broadcast inboxes with per-agent acknowledgements.
- An evidence gate that hashes declared workspace artifacts, rejects scope
  escapes, scans high-confidence secret signatures, and blocks completion until
  every submitted check passes.
- Discoverable model routes for `gpt-5.6-luna`,
  `qwencloud/qwen3.8-max-preview`, `longcat/LongCat-2.0`, and
  `opencode-go/mimo-v2.5`. These are recorded provider routes, not hidden
  substitutions. The host still owns credentials and execution; a slot is
  data, not a credential.
- Local stdio transport for the easiest client install and optional
  authenticated Streamable HTTP at `POST /mcp` for two remote clients.
- Per-agent actor credentials for the team/work plane. A new identity receives
  its `actor_token` once; Finalisma stores only its SHA-256 hash and protects
  later calls from identity spoofing when actor authentication is required.
- One-time pairing links and resumable session event cursors so two separate
  hosts can join without sharing conversation history or provider credentials.

Finalisma coordinates agents; it does not run arbitrary shell commands from
message payloads and it does not silently start or substitute model providers.

## Launch surface

The repository includes a no-build landing page, deterministic pairing demo,
three focused articles, and a launch kit for design partners, Product Hunt,
and YC. Serve it locally with:

```powershell
python -B .\scripts\finalisma-site.py --port 4173
```

Open `http://127.0.0.1:4173/`. The browser simulation is labeled as a
simulation; use the real MCP quickstart for a live handoff. See
[docs/GO_LIVE.md](docs/GO_LIVE.md), [docs/DEMO_VIDEO.md](docs/DEMO_VIDEO.md),
[docs/PRODUCT_HUNT.md](docs/PRODUCT_HUNT.md), and
[docs/YC_APPLICATION.md](docs/YC_APPLICATION.md).

To prove the real local protocol in one command without connecting a host:

```powershell
python -B .\scripts\finalisma-smoke.py
```

## Quickstart — clone to first verified handoff

**Measured 2026-08-05:** first verified handoff in **0.17s** from a clean temp
workspace (MCP stdio server startup + initialize + register × 2 + pairing
create/preview/join + task create/claim/verify/complete, wall-clock). The
protocol operations themselves run in under 40ms; the remaining time is Python
interpreter and SQLite startup. Human time (reading, copy-pasting, approving
the host prompt) is the real budget — the coordinator is not the bottleneck.

### Prerequisites

- Python 3.11+ on PATH
- Two MCP-capable agent hosts (the same host twice works for testing)
- No package install, no global state, no API key

### 1. Start the coordinator

From the project directory, run the launcher with a disposable workspace:

```powershell
python -B .\scripts\finalisma-mcp.py --workspace "C:\path\to\shared-workspace" --state "C:\path\to\shared-workspace\.finalisma\state.db"
```

Leave this running. It opens a stdio MCP server that both agents will connect
to. For a one-shot proof without hosts, `python -B .\scripts\finalisma-smoke.py`
runs the full handoff in-process and prints `evidence_passed: true`.

### 2. Register two agents

In **Agent A**, call:

```text
finalisma_register_agent(team_id="demo", agent_id="agent-a", name="Planner", role="architect", model="gpt-5.6-luna", capabilities=["planning", "research"])
```

In **Agent B**, call:

```text
finalisma_register_agent(team_id="demo", agent_id="agent-b", name="Builder", role="coding", model="qwencloud/qwen3.8-max-preview", capabilities=["coding", "testing"])
```

Each call returns an `actor_token` **once**. Persist it in that host's secret
storage immediately — Finalisma stores only its SHA-256 hash and will never
return the raw token again. Use Agent A's token on calls attributed to Agent A,
Agent B's for Agent B. Stdio defaults to trusted mode so the token is optional
for local proofs, but passing it makes the identity boundary explicit and is
required by default over HTTP.

### 3. Create a pairing link (Agent A)

```text
finalisma_create_pairing(initiator_id="agent-a", team_id="demo", capabilities_offered=["read", "comment"], actor_token="<agent-a actor token>")
```

Share the returned `join_url` or `bootstrap_prompt` with Agent B. The URL
carries the public pairing ID in the path and the one-time token in the
`#token=` fragment. Send it only to the intended recipient — it is a bearer
capability.

### 4. Join with consent (Agent B)

Preview the link, show the policy to the user, obtain explicit confirmation,
then join. Consent is type-strict: `consent` must be the JSON boolean `true`;
`"false"`, `"yes"`, and `1` are all rejected without consuming the link.

```text
finalisma_pairing_preview(token="<token from #token=>")
finalisma_join_pairing(token="<token from the link>", agent_id="agent-b", model="opencode-go/mimo-v2.5", capabilities=["coding", "testing"], consent=true)
```

For a new `agent-b`, the join result includes a fresh `actor_token` and a
`session_token`. Store both securely; they are returned once. An already
registered Agent B must instead pass its existing `actor_token` and receives
no new credential.

### 5. Create a task (Agent A)

```text
finalisma_create_task(team_id="demo", created_by="agent-a", title="Map the API contract", description="Identify endpoints, risks, and tests", scope=["docs/api.md"], preferred_agent="agent-b", idempotency_key="demo-api-map-v1", actor_token="<agent-a actor token>")
```

The server routes the task and emits a `task.dispatch` envelope. The
`idempotency_key` makes the call safe to retry.

### 6. Claim the task (Agent B)

```text
finalisma_claim_task(team_id="demo", agent_id="agent-b", task_id="<task_id from step 5>", actor_token="<agent-b actor token>")
```

Keep the returned `fencing_token` — you need it for every write to this task.
It prevents stale agents from writing after lease loss.

### 7. Submit evidence (Agent B)

Write your artifact inside the declared scope, then submit it with at least one
check. The evidence gate hashes the artifact, rejects scope escapes, scans for
high-confidence secret signatures, and blocks completion until every check
passes.

```text
finalisma_verify_task(team_id="demo", agent_id="agent-b", task_id="<task_id>", fencing_token="<fencing_token from step 6>", artifact_paths=["docs/api.md"], checks=[{"name": "contract-review", "status": "passed", "evidence": "endpoints mapped, 3 risks identified"}], actor_token="<agent-b actor token>")
```

### 8. Complete the task (Agent B)

Only an evidence-gated task can be closed:

```text
finalisma_complete_task(team_id="demo", agent_id="agent-b", task_id="<task_id>", fencing_token="<fencing_token>", summary="API contract mapped with 3 findings", actor_token="<agent-b actor token>")
```

`finalisma_team_status` shows the final state, active leases, and the audit
trail. The handoff is done.

### MCP server entry

To add Finalisma to each host, paste this into the host's MCP configuration.
Replace the two `C:\ABSOLUTE\PATH` placeholders with real absolute paths. Both
clients must point at the same workspace and state file.

```json
{
  "mcpServers": {
    "finalisma": {
      "command": "python",
      "args": [
        "C:\\ABSOLUTE\\PATH\\Multiplayer-AI\\scripts\\finalisma-mcp.py",
        "--workspace", "C:\\ABSOLUTE\\PATH\\shared-workspace",
        "--state", "C:\\ABSOLUTE\\PATH\\shared-workspace\\.finalisma\\state.db"
      ]
    }
  }
}
```

The exact settings UI or filename varies by host. A client must support MCP to
use this directly; non-MCP products need a native connector. See
[examples/dual-agent.md](examples/dual-agent.md) for the two-agent
interoperability test, [examples/pairing-link.md](examples/pairing-link.md) for
the link-first flow, and [docs/PAIRING_UX.md](docs/PAIRING_UX.md) for the
pairing UX contract.

## Remote HTTP mode

Run one coordinator process on a trusted machine. In the default
`--actor-auth auto` policy, HTTP requires per-agent actor credentials even on
loopback. Localhost is still the safest bind; non-local binds additionally
require a transport bearer token:

```powershell
$env:FINALISMA_HTTP_TOKEN = "use-a-secret-from-your-secret-manager"
python .\scripts\finalisma-mcp.py --transport http --host 127.0.0.1 --port 8787 --workspace "C:\path\to\shared-workspace" --state "C:\path\to\shared-workspace\.finalisma\state.db"
```

Point both clients at `http://127.0.0.1:8787/mcp` using their MCP URL/remote
server setting, and supply the bearer token through that host's secret/header
mechanism. Do not put the token in this repository, an MCP JSON file, task
payloads, or logs. New identities bootstrap once through
`finalisma_register_agent` or an invited pairing; all later team/work-plane
calls pass that identity's `actor_token`. For a network bind, use
TLS/reverse-proxy authentication,
an explicit `--allowed-origin`, a private network, and preferably a fixed
coordinator scope:

```powershell
python .\scripts\finalisma-mcp.py --transport http --host 0.0.0.0 --port 8787 --team-id demo --allowed-origin https://your-agent-host.example --workspace "C:\path\to\shared-workspace" --state "C:\path\to\shared-workspace\.finalisma\state.db"
```

`--team-id` (or `FINALISMA_TEAM_ID`) makes the bearer-authenticated
coordinator reject requests for other teams before they reach storage. It is
a useful single-workspace boundary, not a substitute for OAuth/OIDC in a
multi-tenant hosted service.

`--actor-auth trust` is an explicit development escape hatch for loopback HTTP
only. It prints a security warning because any local client that reaches the
endpoint can act as a registered agent; a non-loopback trusted HTTP bind is
rejected. `--actor-auth required` also works for stdio, and
`FINALISMA_ACTOR_AUTH` sets the same policy through the environment.

Set `FINALISMA_PUBLIC_URL` or pass `--public-url` when generated pairing links
must use a reachable hostname instead of `127.0.0.1`.

The current SQLite runtime is a durable single-node coordinator. Read
[docs/SECURITY_GATES.md](docs/SECURITY_GATES.md) before exposing it to
multi-instance or untrusted public traffic; shared storage, OAuth/OIDC,
distributed rate limiting, and an outbox are required for that deployment tier.

The single-node runtime uses bounded, thread-safe idle SQLite connection pools
to avoid reopening the database for every handoff operation. Long-lived library
callers should use `with FinalismaStore(...) as store:` or call `store.close()`
during shutdown. The locked seven-trial evaluator currently records a 95.31%
weighted-median improvement with unchanged semantic digests; methodology,
commands, and scope limits are in [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

## Credential rotation and schema-v3 migration

Rotate an identity with `finalisma_rotate_agent_credential(team_id, agent_id,
current_token)`. The replacement `actor_token` is returned once, the prior
token becomes invalid in the same SQLite transaction, and only the replacement
SHA-256 hash remains at rest. Update the host's secret storage atomically with
the returned value. If the current token is lost, required mode cannot prove
that identity and will not silently overwrite it.

Opening a schema-v2 database upgrades it to schema v3 but deliberately does not
invent credentials for existing identities. Recover a migrated identity only
from a trusted local stdio/operator context by calling
`finalisma_rotate_agent_credential` without `current_token`, which bootstraps a
new credential, or pair a genuinely new `agent_id` and migrate work to it. Do
not expect re-registration or re-pairing under an existing ID to bypass actor
authentication. Closed or expired sessions still require a new pairing;
session tokens remain separate from actor credentials.

For the startup wedge, activation event, retention loop, metrics, pricing
hypothesis, and 30-day design-partner plan, read
[docs/YC_READINESS.md](docs/YC_READINESS.md).

## Development checks

No package installation is needed:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests scripts
python .\scripts\finalisma-mcp.py --help
python -B .\scripts\finalisma_performance_gate.py --baseline .omx\goals\performance\single-node-coordinator-envelope\baseline.json --runs 7
```

The MCP wire contract is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md).
The install snippets and model/provider boundary are in
[examples/dual-agent.md](examples/dual-agent.md).

## Cleanup

The runtime creates only the state directory you choose. For the default
project-local state, stop the MCP processes and remove `.finalisma` inside the
project/workspace. The source files are removed by deleting this project; no
global packages or startup entries are created.

```powershell
Remove-Item -LiteralPath ".\.finalisma" -Recurse -Force
```

Use the exact state path you supplied if you stored the database elsewhere;
do not delete a shared state file until both agents have stopped.
