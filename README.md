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

## Fastest no-install setup

From this project directory, the launcher is:

```powershell
python .\scripts\finalisma-mcp.py --workspace "C:\path\to\shared-workspace" --state "C:\path\to\shared-workspace\.finalisma\state.db"
```

Add the following server entry to each MCP-capable agent. Replace the two
placeholder paths with absolute paths. Both clients must point at the same
state file when using stdio.

```json
{
  "mcpServers": {
    "finalisma": {
      "command": "python",
      "args": [
        "C:\\path\\to\\Multiplayer-AI\\scripts\\finalisma-mcp.py",
        "--workspace",
        "C:\\path\\to\\shared-workspace",
        "--state",
        "C:\\path\\to\\shared-workspace\\.finalisma\\state.db"
      ]
    }
  }
}
```

The default stdio policy is trusted local-process mode, so the initial examples
still work without passing an actor credential. Even in trusted mode,
`finalisma_register_agent` returns an `actor_token` only on the identity's first
registration. Store that raw token in the agent host's secret storage; the
SQLite state contains only its SHA-256 hash. Supplying an `actor_token` in
trusted mode opts that call into credential validation, and starting stdio with
`--actor-auth required` makes it mandatory.

The exact settings UI/file differs by host; use the host's MCP server or
stdio server field and paste the same command/arguments. A client must
support MCP to use this entry directly. For non-MCP products, the protocol
document is the adapter contract; a native connector is still required.

## First two-agent handshake

In each agent, call `finalisma_register_agent` with the same `team_id` and a
unique `agent_id`, then persist the one-time `actor_token` returned for that new
identity. Use Agent A's token for calls attributed to Agent A and Agent B's for
calls attributed to Agent B. Then:

1. Agent A calls `finalisma_create_task`.
2. The server routes it and emits a `task.dispatch` envelope.
3. Agent B calls `finalisma_read_inbox`, then `finalisma_claim_task`.
4. Agent B sends progress with `finalisma_update_task` and can ask Agent A a
   question using `finalisma_send_message`.
5. Agent B submits artifact/check evidence with `finalisma_verify_task`.
6. Only a verified task can be closed with `finalisma_complete_task`.

Call `finalisma_heartbeat` during long work. `finalisma_team_status` shows
stale agents, leases, tasks, and optionally the audit events. In actor-auth
required mode, every team/work-plane call includes the matching `actor_token`;
an HTTP bearer token authenticates the transport but does not replace this
per-agent proof.

## Link-first pairing

Agent A can create a link after registering:

```text
finalisma_create_pairing(
  initiator_id="agent-a",
  team_id="demo",
  capabilities_offered=["read", "comment"],
  actor_token="<agent-a actor token>"
)
```

Share the returned `join_url` or `bootstrap_prompt` with Agent B. Agent B must
preview the link, obtain explicit consent, and call `finalisma_join_pairing`.
Consent is type-strict: `consent` must be the JSON boolean `true`; strings and
numbers such as `"false"`, `"yes"`, or `1` are rejected without consuming the
pairing link.
Generated HTTP links contain only the public pairing ID in the path and keep
the one-time token in the `#token=` fragment; the join adapter sends that token
in the POST body. Token-bearing URL paths are rejected. A newly invited
`agent_id` receives its own `actor_token` once as part of the join result. An
already registered identity must prove possession by passing its existing
`actor_token`; pairing cannot overwrite that identity. Store the actor token
separately from the returned member-bound session token. The session token
enables `finalisma_session_send`,
`finalisma_session_wait`, `finalisma_session_poll`, and
`finalisma_session_ack`. Events are ordered, idempotent, and replayable after a
disconnect. See [docs/PRODUCTION_PROTOCOL.md](docs/PRODUCTION_PROTOCOL.md),
[docs/PAIRING_UX.md](docs/PAIRING_UX.md), and
[examples/pairing-link.md](examples/pairing-link.md).

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
