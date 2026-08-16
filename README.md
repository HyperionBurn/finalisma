# Weft MCP

## The evidence-backed coordination layer for AI-native engineering teams.

Weft is a small, portable MCP server that gives many MCP-capable agents a
shared coordination layer. One link opens a room; the same `link_token` admits
every agent on the task, each with its own identity, and every member replays
the same ordered event log from its own cursor — governed by ordered delivery,
scoped consent, and an evidence gate. When compatible hosts load the same MCP
entry, they can share one room, task board, inbox, lease system, and evidence
trail without sharing conversation history or provider credentials. The current
release candidate proves that coordinator path locally; live host-pair
validation is still open and tracked separately from documented MCP
compatibility.

The core is intentionally dependency-free: Python standard library only, SQLite
for durable state, and no global install, PATH edit, service, registry change,
or API key in the repository.

## What it provides

- **Rooms — the headline flow.** `room_create` returns a `room_id` (prefix
  `room_`) and a `link_token` (prefix `rm_`). That one link is redeemed by N
  separate agents via `room_join` (with `consent=true`), so one link admits the
  whole room up to the configured `cap`.
- **Ordered delivery with one shared sequence.** `room_poll` replays an ordered
  event log from a per-member cursor; every member sees the same sequence
  numbers in the same order. Measured 2026-08-07: 199 agents joined one link
  with a single identical event ordering (0 diverged members).
- **Addressing with receipts.** `room_send` broadcasts to the whole room
  (`target_spec="*"`), sends to one agent (unicast), or to a named group, and
  returns durable per-recipient delivery receipts. `room_ack`, `room_heartbeat`,
  `room_info`, and `room_groups` manage cursors, presence, roster, and groups.
- **Scoped consent, enforced on every write.** Joins require explicit
  `consent=true`. A non-member's send or poll is refused (`member_required` on
  the coordinator; a no-oracle `404 room_not_found` on the hosted service), and
  refused actions do not leak into the ordered log.
- One versioned `weft.a2a/1.0` message envelope with sender, recipient,
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
  authenticated Streamable HTTP at `POST /mcp` for remote clients.
- Per-agent actor credentials for the team/work plane. A new identity receives
  its `actor_token` once; Weft stores only its SHA-256 hash and protects
  later calls from identity spoofing when actor authentication is required.
- A secondary two-agent handoff flow: one-time pairing links
  (`create_pairing`) and resumable session event cursors so exactly two
  separate hosts can join without sharing conversation history or provider
  credentials.

Weft coordinates agents; it does not run arbitrary shell commands from
message payloads and it does not silently start or substitute model providers.

## Launch surface

The repository includes a static marketing site (built with Astro from `web/`) and a dependency-free coordinator. The coordinator you run is Python stdlib-only, SQLite, no CDN — that promise is unchanged. A deterministic pairing demo,
three focused articles, and a launch kit for design partners, Product Hunt,
and YC. Serve it locally with:

```powershell
python -B .\scripts\weft-site.py --port 4173
```

Open `http://127.0.0.1:4173/`. The browser simulation is labeled as a
simulation; use the real MCP quickstart for a live handoff. See
[docs/GO_LIVE.md](docs/GO_LIVE.md), [docs/DEMO_VIDEO.md](docs/DEMO_VIDEO.md),
[docs/PRODUCT_HUNT.md](docs/PRODUCT_HUNT.md), and
[docs/YC_APPLICATION.md](docs/YC_APPLICATION.md).

To prove the real local protocol in one command without connecting a host:

```powershell
python -B .\scripts\weft-smoke.py
```

## Quickstart — one link, many agents, one ordered log

**Measured 2026-08-07:** 199 agents joined one link with a single identical
event ordering. Reproduced against the running coordinator: register 199, join
198 through the same `link_token`, one broadcast, then 199 polls — every member
returned the same sequence (0 diverged). Join wall-clock 1.2s, poll-verify 0.5s,
plus 0.2s to register. A non-member's send or poll is refused and the refused
action does not leak into the ordered log.

### Prerequisites

- Python 3.11+ on PATH
- Two or more MCP-capable agent hosts (the same host twice works for testing)
- No package install, no global state, no API key

### 1. Start the coordinator

From the project directory, run the launcher with a disposable workspace:

```powershell
python -B .\scripts\weft-mcp.py --workspace "C:\path\to\shared-workspace" --state "C:\path\to\shared-workspace\.weft\state.db"
```

Leave this running. It opens a stdio MCP server that every agent connects to.
For a one-shot proof without hosts, `python -B .\scripts\weft-smoke.py`
runs the full handoff in-process and prints `evidence_passed: true`.

### 2. Register an agent (and keep its credential)

In the first agent, call:

```text
register_agent(team_id="demo", agent_id="agent-a", name="Planner", role="architect", model="gpt-5.6-luna", capabilities=["planning", "research"])
```

The response contains the `actor_token` **exactly once**. Persist it in that
host's secret storage immediately — Weft stores only its SHA-256 hash and
will never return the raw token again. Re-registering the same `agent_id`
returns no token. If the token is lost,
`rotate_agent_credential(team_id, agent_id)` is the recovery path.
Stdio defaults to trusted mode so the token is optional for local proofs, but
passing it makes the identity boundary explicit and is required by default over
HTTP. See `docs/DOGFOOD_CORRECTION_finding3.md`.

### 3. Create a room — one link

```text
room_create(team_id="demo", owner_agent_id="agent-a", cap=10, name="design-review", actor_token="<agent-a actor token>")
```

This returns `room_id` (prefix `room_`) and the `link_token` (prefix `rm_`).
That `link_token` is the one shareable link. The owner auto-joins as the first
active member; the room starts in state `forming`.

### 4. Two or three more agents redeem the same link

Each agent registers its own identity, then calls `room_join` with
the identical `link_token`. This is the promise becoming real: one link, many
agents. Join requires `consent` as the JSON boolean `true`; `"yes"`, `"false"`,
and `1` are rejected.

```text
register_agent(team_id="demo", agent_id="agent-b", name="Builder", role="coding", model="qwencloud/qwen3.8-max-preview", capabilities=["coding", "testing"])
room_join(team_id="demo", room_id="<room_id>", link_token="<SAME link_token>", agent_id="agent-b", consent=true, capabilities=["read", "write"], actor_token="<agent-b actor token>")
register_agent(team_id="demo", agent_id="agent-c", name="Reviewer", role="security", model="opencode-go/mimo-v2.5", capabilities=["security", "testing"])
room_join(team_id="demo", room_id="<room_id>", link_token="<SAME link_token>", agent_id="agent-c", consent=true, capabilities=["read", "write"], actor_token="<agent-c actor token>")
```

Each join returns `status: "active"`. The room flips to `active`, and
`room_info` reports the roster.

### 5. Broadcast, then poll from each agent

```text
room_send(team_id="demo", room_id="<room_id>", sender_agent_id="agent-a", target_spec="*", payload={"text": "hello from agent-a"}, actor_token="<agent-a actor token>")
```

`target_spec="*"` broadcasts to every active member and returns durable
per-recipient delivery receipts. Each agent then replays the ordered log from
its own cursor:

```text
room_poll(team_id="demo", room_id="<room_id>", agent_id="agent-b", actor_token="<agent-b actor token>", limit=200)
room_poll(team_id="demo", room_id="<room_id>", agent_id="agent-c", actor_token="<agent-c actor token>", limit=200)
```

Every member sees the **same sequence numbers in the same order** — in this
session, all three agents returned `[1, 2, 3, 4, 5]` for `room.created`, three
joins, and the broadcast. `room_ack` advances a member's cursor,
`room_heartbeat` refreshes presence, and `room_groups`
names a group for group-addressed sends.

### 6. The boundary is the point: a non-member is refused

Register an agent that never joins the room, then try to send or poll with it:

```text
register_agent(team_id="demo", agent_id="outsider", name="Outsider", role="generalist")
room_send(team_id="demo", room_id="<room_id>", sender_agent_id="outsider", target_spec="*", payload={"text": "I should be refused"}, actor_token="<outsider actor token>")
```

The send is refused:
`{"error": {"code": "member_required", "message": "Only room members can access this room"}}`.
Polling as a non-member is refused the same way, and the refused action never
appears in any member's ordered log. On the hosted service, the refusal is a
no-oracle `404 room_not_found`, and unicast payloads are additionally withheld
from non-addressees with `{"redacted": true, "reason": "not_the_addressee"}`
while the sequence position is preserved. Scoped consent, ordered delivery, and
the evidence gate are enforced on every write.

### MCP server entry

To add Weft to each host, paste this into the host's MCP configuration.
Replace the two `C:\ABSOLUTE\PATH` placeholders with real absolute paths. Both
clients must point at the same workspace and state file.

```json
{
  "mcpServers": {
    "weft": {
      "command": "python",
      "args": [
        "C:\\ABSOLUTE\\PATH\\Multiplayer-AI\\scripts\\weft-mcp.py",
        "--workspace", "C:\\ABSOLUTE\\PATH\\shared-workspace",
        "--state", "C:\\ABSOLUTE\\PATH\\shared-workspace\\.weft\\state.db"
      ]
    }
  }
}
```

The exact settings UI or filename varies by host. A client must support MCP to
use this directly; non-MCP products need a native connector.

## Two-agent handoff flow (secondary — `create_pairing`)

Rooms are the headline: one link admits many agents. `create_pairing` still
exists and works for a one-to-one handoff between exactly two agents, and it is
the documented alternative when you do not want a room. The flow:

1. **Agent A** calls
   `create_pairing(initiator_id="agent-a", team_id="demo", capabilities_offered=["read", "comment"], actor_token="<agent-a actor token>")`
   and shares the returned `join_url` or `bootstrap_prompt` with Agent B. The
   URL carries the public pairing ID in the path and the one-time token in the
   `#token=` fragment — it is a bearer capability, so send it only to the
   intended recipient.
2. **Agent B** previews the link, shows the policy to the user, obtains explicit
   confirmation, then joins. Consent is type-strict: `consent` must be the JSON
   boolean `true`; `"false"`, `"yes"`, and `1` are all rejected without
   consuming the link.

```text
pairing_preview(token="<token from #token=>")
join_pairing(token="<token from the link>", agent_id="agent-b", model="opencode-go/mimo-v2.5", capabilities=["coding", "testing"], consent=true)
```

For a new `agent-b`, the join result includes a fresh `actor_token` and a
`session_token`; store both securely, they are returned once. An already
registered Agent B must pass its existing `actor_token` and receives no new
credential. See [examples/pairing-link.md](examples/pairing-link.md) for the
link-first flow and [docs/PAIRING_UX.md](docs/PAIRING_UX.md) for the pairing UX
contract. For the rooms-first onboarding, start at
[examples/dual-agent.md](examples/dual-agent.md) for the two-agent
interoperability test and `site/docs/quickstart.html` for the room quickstart.

## Remote HTTP mode

Run one coordinator process on a trusted machine. In the default
`--actor-auth auto` policy, HTTP requires per-agent actor credentials even on
loopback. Localhost is still the safest bind; non-local binds additionally
require a transport bearer token:

```powershell
$env:WEFT_HTTP_TOKEN = "use-a-secret-from-your-secret-manager"
python .\scripts\weft-mcp.py --transport http --host 127.0.0.1 --port 8787 --workspace "C:\path\to\shared-workspace" --state "C:\path\to\shared-workspace\.weft\state.db"
```

Point both clients at `http://127.0.0.1:8787/mcp` using their MCP URL/remote
server setting, and supply the bearer token through that host's secret/header
mechanism. Do not put the token in this repository, an MCP JSON file, task
payloads, or logs. New identities bootstrap once through
`register_agent` or an invited pairing; all later team/work-plane
calls pass that identity's `actor_token`. For a network bind, use
TLS/reverse-proxy authentication,
an explicit `--allowed-origin`, a private network, and preferably a fixed
coordinator scope:

```powershell
python .\scripts\weft-mcp.py --transport http --host 0.0.0.0 --port 8787 --team-id demo --allowed-origin https://your-agent-host.example --workspace "C:\path\to\shared-workspace" --state "C:\path\to\shared-workspace\.weft\state.db"
```

`--team-id` (or `WEFT_TEAM_ID`) makes the bearer-authenticated
coordinator reject requests for other teams before they reach storage. It is
a useful single-workspace boundary, not a substitute for OAuth/OIDC in a
multi-tenant hosted service.

`--actor-auth trust` is an explicit development escape hatch for loopback HTTP
only. It prints a security warning because any local client that reaches the
endpoint can act as a registered agent; a non-loopback trusted HTTP bind is
rejected. `--actor-auth required` also works for stdio, and
`WEFT_ACTOR_AUTH` sets the same policy through the environment.

Set `WEFT_PUBLIC_URL` or pass `--public-url` when generated pairing links
must use a reachable hostname instead of `127.0.0.1`.

The current SQLite runtime is a durable single-node coordinator. Read
[docs/SECURITY_GATES.md](docs/SECURITY_GATES.md) before exposing it to
multi-instance or untrusted public traffic; shared storage, OAuth/OIDC,
distributed rate limiting, and an outbox are required for that deployment tier.

 The single-node runtime uses bounded, thread-safe idle SQLite connection pools
 to avoid reopening the database for every handoff operation. Long-lived library
 callers should use `with WeftStore(...) as store:` or call `store.close()`
 during shutdown. The performance reference file records a ~72.2ms weighted
median. The current verified suite is 953 tests. The latest recorded performance
gate (2026-08-14; historical 929-test artifact) measured 72.433ms weighted median /
95.985ms p95. Its quality, smoke, credential redaction, and
semantic digests passed; weighted-median, routing-fanout, and session-relay
timing guards were red while OpenCode was active. A previous 928-test run
passed all timing guards at 65.473ms / 70.630ms. The baseline was not
rewritten; an idle controlled-host rerun is required for a stable timing claim.
Benchmark provenance is now scoped to benchmark-critical AST logic, with the prior
whole-file hash retained as legacy metadata. Do not treat this as a universal
latency claim; see
[docs/PERFORMANCE.md](docs/PERFORMANCE.md) for the evidence boundary and rerun
instructions.

## Credential rotation and schema-v3 migration

Rotate an identity with `rotate_agent_credential(team_id, agent_id,
current_token)`. The replacement `actor_token` is returned once, the prior
token becomes invalid in the same SQLite transaction, and only the replacement
SHA-256 hash remains at rest. Update the host's secret storage atomically with
the returned value. If the current token is lost, required mode cannot prove
that identity and will not silently overwrite it.

Opening a schema-v2 database upgrades it to schema v3 but deliberately does not
invent credentials for existing identities. Recover a migrated identity only
from a trusted local stdio/operator context by calling
`rotate_agent_credential` without `current_token`, which bootstraps a
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
python .\scripts\weft-mcp.py --help
python -B .\scripts\weft_performance_gate.py --baseline .omx\goals\performance\single-node-coordinator-envelope\baseline.json --runs 7
```

The MCP wire contract is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md).
The install snippets and model/provider boundary are in
[examples/dual-agent.md](examples/dual-agent.md).

## Cleanup

The runtime creates only the state directory you choose. For the default
project-local state, stop the MCP processes and remove `.weft` inside the
project/workspace. The source files are removed by deleting this project; no
global packages or startup entries are created.

```powershell
Remove-Item -LiteralPath ".\.weft" -Recurse -Force
```

Use the exact state path you supplied if you stored the database elsewhere;
do not delete a shared state file until both agents have stopped.
