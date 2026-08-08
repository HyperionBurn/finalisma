# SDK-Tier Interop Validation — 2026-08-05

**Tier:** SDK (`weft_sdk.WeftClient`) — anything that can run Python.
**Transport:** Streamable HTTP (`http.client`, stdlib-only), coordinator spawned as a child over `--transport http`.
**Driver:** `scripts/interop-validate-sdk.py` — Python driver owns the coordinator child via `subprocess.Popen` with `finally` teardown. Run under `timeout 240`.
**Tests:** `tests/test_interop_sdk.py` — 5 unittest cases, all green in ~1.1s.

## Honest status

**This proves the SDK tier works end to end, including the N-agent room path, against a real coordinator over HTTP.** Three Python "hosts" each use `WeftClient` as their client; room tools are reached via the SDK's private `_call("room_*", ...)` (the SDK has no dedicated room helper, so `_call` is the documented escape hatch — it dispatches any tool over JSON-RPC and injects `team_id`/`agent_id`/`actor_token`).

**What is proven:**
- SDK registration with actor-token binding (token returned once, stored on the client).
- Two-party pairing + verified handoff through SDK dedicated methods (`create_pairing_link`, `claim`, `submit_evidence`, `complete`) plus `_call`-driven `join_pairing`.
- N-agent room: 3 agents join one room link; roster shows all 3; state transitions forming→active.
- Addressing: unicast (receipt for exactly the target), group (create via `room_groups` add, send to group name, receipts for the two members), broadcast `*` (receipts for the other two, sender excluded).
- Ordered event log + per-member cursors: each member polls from `after_seq=0`, events ascend by `seq`, no duplicate seqs, ack advances a monotonic cursor.
- Reconnect: a fresh `WeftClient` reusing agent-b's `agent_id`+`actor_token` polls from `after_seq=0` → full ordered log with no loss/duplicates; polls from `last_ack_seq` → zero already-acked events replayed.
- Negative cases: non-member `room_poll` and `room_info` refused with `member_required`; existing-member identity reuse with a **different** actor token refused with `actor_auth_invalid`.

> **Superseded 2026-08-08 (enumeration fix):** a non-member is now refused with `room_not_found`
> on every member-only room tool — identical to a fabricated `room_id` (no existence oracle).
> `member_required` is retained only for entitled members (e.g. a member adding a non-active
> *target* to a group). See `docs/ROOMS_DESIGN.md` §8/§9.

**What is NOT proven:**
- A third-party non-Python client using the SDK. The SDK is Python-only (`src/weft_sdk/` is Python); this validation uses Python hosts. The *protocol* (Streamable HTTP + JSON-RPC `tools/call`) is language-agnostic, but no non-Python client is tested here.
- The SDK's public `join_pairing()` helper does **not** inject `actor_token` (a real gap — the driver works around it via `_call`). This is a documented SDK limitation, not a coordinator defect.
- Rooms via the SDK's dedicated API surface — the SDK has no `room_*` methods yet; `_call` is the only path.

## Host / version

```
Python 3.14.6
Weft coordinator: src/weft_mcp/ (stdlib-only, SQLite)
SDK: src/weft_sdk/client.py (WeftClient)
Transport: http.client → http://127.0.0.1:<free-port>/mcp
```

## Exact commands (redacted)

```bash
# Pick a free port by binding socket(0); the driver does this internally.
timeout 240 python -B scripts/interop-validate-sdk.py
python -B -m unittest tests.test_interop_sdk -v
netstat -ano | grep <port>   # must show NO LISTENER after exit (only TIME_WAIT)
python -B scripts/weft-smoke.py
```

## Full verbatim transcript (room path + key results)

```
# free port: 52970
# coordinator TCP-accepting
# register agent-a: token=fst_ac...jdtA
# register agent-b: token=fst_ac...vs8Q
# register agent-c: token=fst_ac...Ew1c
# register agent-outsider: token=fst_ac...HprQ
# protocol: None / None
=== PART A: two-party pairing + verified handoff ===
# pairing link created: pairing_id=pair_34ff057d3d934a1c876959a1bdceb26f token=fst_pa...5R_k
# agent-b joined pairing: state=active
  PASS  pairing join state active/open
  PASS  pairing #2 join state active/open
# task created: task_1ac3ead55da944eea832f7845317f054
  PASS  task created
# task claimed by agent-b: fencing=1812495659374878
  PASS  claim returns fencing token
# evidence submitted: passed=True
  PASS  evidence gate passed
# task completed: status=done
  PASS  task completed (done)
=== PART B: N-agent room (product claim) ===
# room created: room_id=room_8cc840e0fd9044b7ab2154af9e9c1222 cap=4 state=forming
  PASS  room created in forming state
  PASS  room cap >= 4
# agent-b joined room: status=active cursor=0
  PASS  agent-b joined room active
# agent-c joined room: status=active cursor=0
  PASS  agent-c joined room active
# room info: state=active members=['agent-a', 'agent-b', 'agent-c'] count=3
  PASS  room has 3 members
  PASS  member roster is {a,b,c}
  PASS  room state active after joins
# unicast A->B: receipts=['agent-b'] seq=5
  PASS  unicast receipt targets exactly [agent-b]
  PASS  unicast receipts queued
# group 'builders' members=['agent-b', 'agent-c']
  PASS  group builders = {b,c}
# group send: receipts=['agent-b', 'agent-c']
  PASS  group receipt targets [agent-b, agent-c]
# broadcast *: receipts=['agent-b', 'agent-c']
  PASS  broadcast receipt targets [agent-b, agent-c]
# agent-a poll(0): 7 events, seqs=[1, 2, 3, 4, 5, 6, 7], next_seq=8
  PASS  agent-a events returned in ascending seq order
  PASS  agent-a no duplicate seqs in poll
# agent-a ack to 7: last_ack_seq=7
  PASS  agent-a cursor advanced to 7
# agent-b poll(0): 7 events, seqs=[1, 2, 3, 4, 5, 6, 7], next_seq=8
  PASS  agent-b events returned in ascending seq order
  PASS  agent-b no duplicate seqs in poll
# agent-b ack to 7: last_ack_seq=7
  PASS  agent-b cursor advanced to 7
# agent-c poll(0): 7 events, seqs=[1, 2, 3, 4, 5, 6, 7], next_seq=8
  PASS  agent-c events returned in ascending seq order
  PASS  agent-c no duplicate seqs in poll
# agent-c ack to 7: last_ack_seq=7
  PASS  agent-c cursor advanced to 7
# reconnect: fresh client reusing agent-b identity + actor_token
# reconnect poll(0): 7 events, seqs=[1, 2, 3, 4, 5, 6, 7]
  PASS  reconnect replay: ordered, no duplicates
# reconnect poll(from cursor 7): 0 new events
  PASS  reconnect from cursor: no already-acked events replayed
=== NEGATIVE cases ===
  PASS  outsider poll refused: code=member_required msg=Only room members can access this room
  PASS  outsider room_poll refused
  PASS  outsider poll error code == member_required (got member_required)
  PASS  outsider room_info refused: code=member_required
  PASS  outsider room_info refused
  PASS  outsider room_info error code == member_required (got member_required)
  PASS  actor-overwrite join refused: code=actor_auth_invalid msg=Actor token is invalid
  PASS  actor-overwrite room_join refused
  PASS  actor-overwrite error code == actor_auth_invalid (got actor_auth_invalid)
```

## Result JSON

```json
{
  "status": "ok",
  "transport": "http",
  "sdk_tier": "weft_sdk.WeftClient over Streamable HTTP",
  "pairing_join_state": "active",
  "task_status": "done",
  "evidence_passed": true,
  "room": {
    "room_id": "room_8cc840e0fd9044b7ab2154af9e9c1222",
    "cap": 4,
    "state": "active",
    "member_count": 3,
    "members": ["agent-a", "agent-b", "agent-c"],
    "unicast_receipts": ["agent-b"],
    "group_receipts": ["agent-b", "agent-c"],
    "broadcast_receipts": ["agent-b", "agent-c"],
    "reconnect_replay_events": 7,
    "reconnect_from_cursor_events": 0
  },
  "negative": {
    "outsider_poll_refused": true,
    "outsider_poll_code": "member_required",
    "outsider_info_refused": true,
    "outsider_info_code": "member_required",
    "actor_overwrite_refused": true,
    "actor_overwrite_code": "actor_auth_invalid"
  },
  "time_total_s": 1.262,
  "time_room_s": 0.422,
  "failures": []
}
```

## Timing

- Total driver runtime: **1.262 s**
- Room path (Part B): **0.422 s**
- Unittest suite: **1.113 s** (5 tests)

## Post-run listener check

`netstat -ano | grep <port>` after exit shows only `TIME_WAIT` client-side sockets — **no LISTENING socket**, coordinator child torn down in `finally` (terminate → wait → kill).

`python -B scripts/weft-smoke.py` → `evidence_passed: true`, `task_status: done`.

## Proven vs not

| Claim | Status |
| --- | --- |
| SDK registration + token binding | PROVEN |
| Two-party pairing + verified handoff via SDK | PROVEN |
| N-agent room (3 join one link) | PROVEN |
| Roster shows all 3, state active | PROVEN |
| Unicast / group / broadcast receipts | PROVEN |
| Ordered replay, per-member cursors | PROVEN |
| Reconnect: no loss, no duplicates | PROVEN |
| Non-member refused (`member_required`) | PROVEN |
| Actor-overwrite refused (`actor_auth_invalid`) | PROVEN |
| Non-Python third-party client via SDK | **NOT PROVEN** (SDK is Python-only) |
| Rooms via SDK dedicated API (not `_call`) | **NOT PROVEN** (SDK has no `room_*` methods) |

## Risks for the orchestrator to check

1. **SDK `join_pairing()` does not inject `actor_token`.** The driver works around it with `_call("join_pairing", ...)`. A real SDK consumer calling the public `join_pairing()` helper against a non-trusted coordinator will get `actor_auth_invalid`. Worth a dedicated `room_*` method set or fixing `join_pairing()` to forward the token.
2. **`register()` does not store the returned token.** The driver re-builds each client with the token. A consumer that calls `register()` and then the client's own methods without re-binding will fail auth. This is a footgun worth documenting or fixing.
3. This validation uses Python hosts; the "any agent" claim (roadmap §3) still needs the bridge-adapter and Streamable-HTTP-remote-host transcripts to be complete. The SDK tier is now proven, but it is one of four.
