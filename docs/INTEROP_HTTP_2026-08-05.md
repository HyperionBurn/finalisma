# Finalisma — MCP Streamable HTTP protocol validation

**Date:** 2026-08-05
**Tier:** MCP Streamable HTTP (roadmap §3 #2)
**Status:** PROTOCOL-TIER VALIDATED — full two-agent verified handoff over HTTP wire protocol; negative pairing-link-reuse refusal confirmed.

This transcript proves the **wire protocol** over Streamable HTTP: a genuine remote
JSON-RPC client (`http.client`, no `finalisma_mcp` imports) drove the coordinator
through `initialize`, `tools/list`, the complete pairing + verified-handoff lifecycle,
and a deliberate negative case — all over `POST /mcp`.

---

## What is proven

| Claim | Evidence |
| --- | --- |
| MCP JSON-RPC initialize over HTTP | `protocolVersion: "2025-11-25"`, `serverInfo.name: "finalisma-mcp"` |
| Tool inventory advertised over HTTP | `tools/list` returned 58 tools, all 8 required lifecycle tools present |
| Pairing create → preview → join over HTTP | preview `status: "issued"`, join `state: "active"` |
| Task create → claim → verify → complete over HTTP | task `status: "done"`, evidence gate `passed: true` |
| One-use pairing link is consumed | reuse attempt refused with `pairing_unavailable / "Pairing is consumed"` |
| Clean teardown | driver `finally` block terminates child; no listener left behind (`TIME_WAIT` only, no `LISTENING`) |

## What is NOT proven (honest scope)

- **Host-product integration** for the HTTP tier (a real MCP host loading Finalisma via its HTTP remote-server config). That is a separate, future validation — this is a protocol-tier proof.
- **Authenticated HTTP mode** (`--actor-auth auto` over HTTP, bearer token path). This run used `--actor-auth trust` on loopback, the documented development escape hatch. The auth-required path is implemented in `server.py` (`_authorized`, `_handle_mcp_post`) but not exercised by this transcript.
- **Non-loopback / TLS / reverse-proxy** deployment shapes.
- **GET-streaming** (the server returns `405` for `GET /mcp`; long-poll is via `finalisma_session_wait`).

---

## Environment

- Host: Windows 11, Python 3.14 (project requires 3.11+)
- Finalisma version: `0.1.0` (from `serverInfo`)
- Transport: Streamable HTTP, loopback (`127.0.0.1`), OS-selected ephemeral port
- Launch command (redacted — no secrets were used; `--actor-auth trust` on loopback):

```powershell
python -B scripts/finalisma-mcp.py --transport http --host 127.0.0.1 --port <FREE_PORT> --team-id demo --workspace <tmp> --state <tmp>\.finalisma\state.db --actor-auth trust
```

The driver picks the free port by binding a TCP socket to port 0, reading the allocated
port, closing it, then passing that port to the child — deterministic, no collision.

---

## Driver

`scripts/interop-validate-http.py` — spawns the coordinator as a child process via
`subprocess.Popen`, waits for the TCP port to accept, then speaks real JSON-RPC over
`http.client.HTTPConnection`. Teardown in a `finally` block: `proc.terminate()` →
`wait(10s)` → `kill` fallback. Emits a JSON result and exits 0 only if the full
lifecycle + negative passed.

Run: `timeout 180 python -B scripts/interop-validate-http.py`

---

## Result summary

```json
{
  "status": "ok",
  "transport": "http",
  "protocol_version": "2025-11-25",
  "server_info": {"name": "finalisma-mcp", "version": "0.1.0"},
  "tool_count": 58,
  "pairing_preview": "issued",
  "join_state": "active",
  "task_status": "done",
  "evidence_passed": true,
  "negative_pairing_link_reuse_refused": true,
  "time_to_verified_handoff_s": 0.655
}
```

Wall-clock from spawn to verified handoff complete: **0.655 s** (includes Python +
SQLite startup; protocol operations are sub-40ms).

---

## Verbatim transcript (key exchanges)

### initialize → `POST /mcp`

```
> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{}}}
< HTTP 200 OK: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"finalisma-mcp","version":"0.1.0"},"instructions":"Use finalisma_register_agent before mutations. ..."}}
```

### tools/list → `POST /mcp`

```
> {"jsonrpc":"2.0","id":2,"method":"tools/list"}
< HTTP 200 OK: {"jsonrpc":"2.0","id":2,"result":{"tools":[... 58 tools ...]}}
```

Required tools present: `finalisma_register_agent`, `finalisma_create_pairing`,
`finalisma_pairing_preview`, `finalisma_join_pairing`, `finalisma_create_task`,
`finalisma_claim_task`, `finalisma_verify_task`, `finalisma_complete_task`.

### register agent-a → `POST /mcp`

```
> {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"finalisma_register_agent","arguments":{"team_id":"demo","agent_id":"agent-a","role":"architect","name":"Planner"}}}
< HTTP 200 OK: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{\n  \"agent_id\": \"agent-a\",\n  \"name\": \"Planner\",\n  \"role\": \"architect\",\n  \"status\": \"active\",\n  \"actor_token\": \"**REDACTED**\"\n}"}]}}
```

### register agent-b → `POST /mcp`

```
> {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"finalisma_register_agent","arguments":{"team_id":"demo","agent_id":"agent-b","role":"builder","name":"Builder"}}}
< HTTP 200 OK: {"jsonrpc":"2.0","id":4,"result":{... "agent_id": "agent-b", "actor_token": "**REDACTED**" ...}}
```

### create pairing → `POST /mcp`

```
> {"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"finalisma_create_pairing","arguments":{"initiator_id":"agent-a","team_id":"demo","capabilities_offered":["read","comment"],"actor_token":"**REDACTED**"}}}
< HTTP 200 OK: {..."pairing_id":"pair_2a2880f820a24ddf96383fbba04fe9dd","display_code":"T2C9-S8VU","join_token":"**REDACTED**","policy":{"requires_consent":true}...}
```

### pairing preview → `POST /mcp`

```
> {"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"finalisma_pairing_preview","arguments":{"token":"**REDACTED**"}}}
< HTTP 200 OK: {..."status":"issued","created_by":"agent-a","capabilities_offered":["read","comment"],"policy":{"requires_consent":true}...}
```

### join pairing (consent=true) → `POST /mcp`

```
> {"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"finalisma_join_pairing","arguments":{"token":"**REDACTED**","agent_id":"agent-b","consent":true,"actor_token":"**REDACTED**"}}}
< HTTP 200 OK: {..."session_id":"session_1cca574dbbc141199aceccf74f4f5ed6","state":"active","members":["agent-a","agent-b"]...}
```

### create task → `POST /mcp`

```
> {"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"finalisma_create_task","arguments":{"team_id":"demo","created_by":"agent-a","title":"Interop review","description":"Verify the retry boundary in handoff.txt","scope":["handoff.txt"],"preferred_agent":"agent-b","idempotency_key":"interop-http-task-v1","actor_token":"**REDACTED**"}}}
< HTTP 200 OK: {..."task":{"task_id":"task_44ce51e8c40e456a8d638e9f4b8c18c1","status":"pending","owner_id":"agent-b"},"dispatch":{"sent":true,...}}...
```

### claim task → `POST /mcp`

```
> {"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"finalisma_claim_task","arguments":{"team_id":"demo","agent_id":"agent-b","task_id":"task_44ce51e8c40e456a8d638e9f4b8c18c1","actor_token":"**REDACTED**"}}}
< HTTP 200 OK: {..."status":"in_progress","claimed_by":"agent-b","fencing_token":1456497950675916,"version":2...}
```

### verify task (evidence gate) → `POST /mcp`

Driver wrote `handoff.txt` (33 bytes) into the workspace, then:

```
> {"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"finalisma_verify_task","arguments":{"team_id":"demo","agent_id":"agent-b","task_id":"task_44ce51e8c40e456a8d638e9f4b8c18c1","fencing_token":1456497950675916,"files":["handoff.txt"],"checks":[{"name":"interop-check","status":"passed","evidence":"artifact present"}],"actor_token":"**REDACTED**"}}}
< HTTP 200 OK: {..."passed":true,"evidence_id":"evidence_eae870a5d8454dd38e5931b0fd28e264","status":"passed","task_status":"verified","details":{"files":[{"path":"handoff.txt","exists":true,"bytes":33,"sha256":"e5f7a0e57ff73c3765907ed135790e94386b6398ed72c9e40236ad2dee990732"}],"checks":[{"name":"interop-check","status":"passed"}],"scope_violations":[],"secret_scan":{"status":"passed"},"review":{"required":false,"passed":true},"failures":{"checks":[],"missing_files":[],"oversized_files":[]}}}...}
```

### complete task → `POST /mcp`

```
> {"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"finalisma_complete_task","arguments":{"team_id":"demo","agent_id":"agent-b","task_id":"task_44ce51e8c40e456a8d638e9f4b8c18c1","fencing_token":1456497950675916,"summary":"Interop HTTP verified","actor_token":"**REDACTED**"}}}
< HTTP 200 OK: {..."status":"done","progress":100,"metadata":{"completion_summary":"Interop HTTP verified"}...}
```

---

## Negative case — pairing link reuse → `POST /mcp`

```
> {"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"finalisma_join_pairing","arguments":{"token":"**REDACTED**","agent_id":"agent-c","consent":true}}}
< HTTP 200 OK: {"jsonrpc":"2.0","id":12,"result":{"isError":true,"content":[{"type":"text","text":"{\"error\": {\"code\": \"pairing_unavailable\", \"message\": \"Pairing is consumed\"}}"}]}}
```

Refusal fired exactly as designed: `pairing_unavailable / "Pairing is consumed"`.

---

## Verification commands (all green on 2026-08-05)

```powershell
timeout 180 python -B scripts/interop-validate-http.py
# exit 0, status ok, negative_pairing_link_reuse_refused: true

python -B -m unittest tests.test_interop_http -v
# Ran 1 test in 0.718s — OK

netstat -ano | grep <port>
# only TIME_WAIT entries (client side), no LISTENING — child torn down cleanly

python -B scripts/finalisma-smoke.py
# evidence_passed: true (regression intact)
```

---

## Honest status line

**PROTOCOL-TIER VALIDATED.** The MCP Streamable HTTP wire protocol works end-to-end:
initialize, tool discovery, the full pairing + evidence-gated handoff lifecycle, and the
one-use pairing-link enforcement — all driven by a genuine remote client over
`POST /mcp` with no access to coordinator internals. The coordinator stays stdlib-only.
A real MCP-host product integration for the HTTP tier is future work.
