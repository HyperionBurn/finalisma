# Weft SDK

A stdlib-only Python client for the [Weft A2A protocol](https://github.com/Weft/weft-mcp).
Talks JSON-RPC over HTTP to a `weft-mcp` coordinator using only `http.client`.
No third-party dependencies. Python 3.11+.

## Install

The SDK lives inside the Weft monorepo at `src/weft_sdk/`.  It ships
with the coordinator — no separate `pip install` needed.  Add the repo's `src/`
to `PYTHONPATH` or install the workspace with `pip install -e .`.

## Quickstart: pair two agents in ~20 lines

```python
from weft_sdk import WeftClient

COORDINATOR = "http://127.0.0.1:8787/mcp"

# Agent A registers and creates a pairing link
a = WeftClient(COORDINATOR, "agent-a", "demo")
reg_a = a.register(name="Planner", role="architect", capabilities=["planning"])
a._actor_token = reg_a["actor_token"]          # persist securely
pairing = a.create_pairing_link(capabilities=["read", "comment"])
print("Share this link with Agent B:", pairing.join_url)

# Agent B joins the pairing (extracts token from URL fragment automatically)
b = WeftClient(COORDINATOR, "agent-b", "demo")
join = b.join_pairing(pairing.join_url, consent=True)
b._actor_token = join.actor_token              # persist securely

# A creates a task, B claims and works it
task_id = a.create_task(scope=["src/api.py"], description="Implement /health", title="Implement /health")
task = b.claim(task_id)
b.update_progress(task_id, pct=50, note="half done", fencing_token=task.fencing_token)
b.submit_evidence(task_id, ["src/api.py"],
                  checks=[{"name": "tests", "status": "passed"}],
                  fencing_token=task.fencing_token)
b.complete(task_id, fencing_token=task.fencing_token, summary="Shipped")
```

## Feature table

| Capability | SDK method | Notes |
|---|---|---|
| Connectivity | `connect()` | Returns protocol info; health check. |
| Identity | `register()`, `heartbeat()`, `rotate_credential()` | Token auto-stored; env `WEFT_ACTOR_TOKEN` supported. |
| Pairing | `create_pairing_link()`, `join_pairing(link)` | Fragment-token handling is automatic. |
| Tasks | `create_task()`, `claim()`, `update_progress()`, `submit_evidence()`, `complete()` | Typed `TaskResult` with `fencing_token`. |
| Messaging | `ask()`, `send_envelope()` | Envelopes are arbitrary dicts. |
| Sessions | `session_send()`, `session_poll()`, `session_wait()`, `session_ack()` | Ordered, idempotent, replayable events. |
| Errors | `WeftError`, `AuthError`, `EvidenceError`, `NotFoundError`, `ConflictError`, `TimeoutError` | Mapped from server error codes. |
| Retry | stdlib exponential backoff | Idempotent methods retry on 408/429/5xx with idempotency keys. |

## SDK API surface

```
WeftClient(coordinator_url, agent_id, team_id,
                actor_token=None,    # or WEFT_ACTOR_TOKEN env var
                bearer_token=None,   # transport-level HTTP bearer
                timeout=30.0)
  .connect() -> dict
  .register(name, role, model, capabilities, metadata) -> dict
  .heartbeat(task_ids, fencing_tokens) -> dict
  .rotate_credential(current_token) -> CredentialRotation
  .create_pairing_link(capabilities, ttl_seconds) -> PairingResult
  .join_pairing(link, consent, agent_id) -> JoinResult
  .create_task(scope, description, priority, capabilities, title, idempotency_key) -> task_id: str
  .claim(task_id, lease_seconds) -> TaskResult
  .update_progress(task_id, pct, note, fencing_token) -> TaskResult
  .submit_evidence(task_id, artifact_paths, checks, fencing_token) -> dict
  .complete(task_id, fencing_token, summary) -> TaskResult
  .ask(recipient, text, kind) -> dict
  .send_envelope(envelope_dict) -> dict
  .session_send(session_token, kind, payload, idempotency_key, trace_id, agent_id) -> SessionEvent
  .session_poll(session_token, after_seq, limit, agent_id) -> list[SessionEvent]
  .session_wait(session_token, after_seq, timeout_seconds, limit, agent_id) -> list[SessionEvent]
  .session_ack(session_token, seq, agent_id) -> int
  .close()
```

## Token hygiene

- `actor_token` is accepted as a constructor argument **or** via the
  `WEFT_ACTOR_TOKEN` environment variable.
- Tokens are **never logged** and **never appear in `__repr__`**.
- After `rotate_credential()`, the client updates its stored token atomically.
- Always persist returned tokens in your host's secret storage; the server
  stores only SHA-256 hashes.

## Retry & idempotency

All mutating calls carry an auto-generated `idempotency_key` and retry up to
4 times with stdlib-only exponential backoff on transient HTTP failures
(408, 429, 500, 502, 503, 504).  Non-idempotent calls (`session_send`,
`join_pairing`, `close_session`) are NOT retried.

## Error mapping

Server error codes are mapped to typed exceptions:

| Server code | SDK exception |
|---|---|
| `actor_auth_required`, `actor_auth_invalid`, `consent_required`, `session_unauthorized`, `session_forbidden` | `AuthError` |
| `quality_gate_required`, `quality_gate_failed` | `EvidenceError` |
| `pairing_not_found`, `task_not_found`, `message_not_found`, `agent_not_registered` | `NotFoundError` |
| `pairing_expired`, `pairing_unavailable`, `pairing_race`, `task_claim_conflict`, `scope_lock_conflict`, `stale_fencing_token`, `lease_expired`, `state_conflict` | `ConflictError` |
| Transport timeout | `TimeoutError` |
| Anything else | `WeftError` |

## Security notes

- **Plaintext HTTP**: the SDK talks HTTP.  Put a TLS-terminating reverse proxy
  in front for any non-loopback coordinator.
- **Bearer token**: pass `bearer_token=` to enable HTTP bearer auth at the
  transport layer.  This is separate from the per-agent `actor_token`.
- **Pairing URLs**: the one-time token lives only in the URL `#fragment`.
  `join_pairing()` extracts it and sends it in the POST body.  Never log the
  full URL.
- **No secrets in code**: use environment variables or a secret manager for
  tokens; never hard-code them.

## Rooms — one link, N agents

A **room** is a multi-member conversation: one multi-use link admits **N** agents
(up to a cap), each with its own identity, an ordered event log, per-member
cursors, and addressing by unicast, named group, or broadcast. A room survives
disconnects — a member reconnects with the same `agent_id` + `actor_token` and
resumes from its cursor with **no loss and no duplicates**.

The snippet below is copy-paste-runnable against a coordinator already listening
at `http://127.0.0.1:8787/mcp` (stdio spawn is one line — see the comment at the
top). It uses **only** the public SDK API.

```python
# pragma: no cover — runnable quickstart, not part of the test suite.
#
# Spawn the coordinator in a separate terminal if it is not already running:
#   python -B scripts/weft-mcp.py --transport http --port 8787
#
# Then run this file:
#   python -B docs/rooms_quickstart.py

from weft_sdk import WeftClient

COORDINATOR = "http://127.0.0.1:8787/mcp"
TEAM = "demo"

# --- 1. Three agents register and persist their actor tokens ----------------
a = WeftClient(COORDINATOR, "agent-a", TEAM)
b = WeftClient(COORDINATOR, "agent-b", TEAM)
c = WeftClient(COORDINATOR, "agent-c", TEAM)

reg_a = a.register(name="A", role="coordinator", capabilities=["planning"])
reg_b = b.register(name="B", role="builder", capabilities=["coding"])
reg_c = c.register(name="C", role="builder", capabilities=["coding"])

# actor_token is returned once on registration — persist it and feed it back
# into the constructor on every future run (or via WEFT_ACTOR_TOKEN).
a._actor_token = reg_a["actor_token"]
b._actor_token = reg_b["actor_token"]
c._actor_token = reg_c["actor_token"]

# --- 2. agent-a creates a room with cap=4 -----------------------------------
room = a.create_room(cap=4, name="planning", ttl_seconds=3600)
room_id = room["room_id"]
link_token = room["link_token"]          # multi-use; share out-of-band
print(f"[a] created room {room_id} (cap {room['cap']})")

# --- 3. all three join with the SAME link_token (consent is mandatory) -----
# agent-a is already a member (owner auto-joins); re-join is idempotent.
ja = a.join_room(room_id, link_token, consent=True, capabilities=["planning"])
jb = b.join_room(room_id, link_token, consent=True, capabilities=["coding"])
jc = c.join_room(room_id, link_token, consent=True, capabilities=["coding"])
print(f"[a] join status={ja['status']}  [b]={jb['status']}  [c]={jc['status']}")

# --- 4. addressing: unicast, group, broadcast ------------------------------
# Unicast: a -> b
send_uni = a.send(room_id, target="agent-b",
                  payload={"text": "please start the API"},
                  exclude_sender=True)
print(f"[a] unicast -> b, seq={send_uni['seq']}")

# Group: a addresses the "builders" group (b and c)
a.add_to_group(room_id, "builders", ["agent-b", "agent-c"])
send_grp = a.send(room_id, target="builders",
                  payload={"text": "build the /health endpoint"})
print(f"[a] group -> builders, seq={send_grp['seq']}")

# broadcast: a -> every other member
send_all = a.send(room_id, target="*",
                  payload={"text": "sync at 14:00"})
print(f"[a] broadcast '*', seq={send_all['seq']}")

# --- 5. ordered poll from a per-member cursor ------------------------------
poll_b = b.room_poll(room_id, after_seq=0)
print(f"[b] poll got {len(poll_b['events'])} events, "
      f"next_seq={poll_b['next_seq']}, has_more={poll_b['has_more']}")
for ev in poll_b["events"]:
    print(f"    seq={ev['seq']} kind={ev['kind']} origin={ev['origin_agent']}")

# advance b's cursor — monotonic ack, replays are idempotent
last = poll_b["events"][-1]["seq"]
acked = b.room_ack(room_id, seq=last)
print(f"[b] ack -> last_ack_seq={acked['last_ack_seq']}")

# --- 6. RECONNECT: fresh client, same identity, no loss -------------------
# agent-b's process crashes. A brand-new client constructed with the SAME
# agent_id + actor_token resumes from the last ack and sees nothing twice.
b2 = WeftClient(COORDINATOR, "agent-b", TEAM, actor_token=reg_b["actor_token"])
resume = b2.room_poll(room_id)            # after_seq defaults to last_ack_seq
print(f"[b'] reconnect poll got {len(resume['events'])} new events "
      f"(last_ack_seq was {resume['last_ack_seq']})")
assert resume["events"] == [] or resume["events"][0]["seq"] > acked["last_ack_seq"], \
    "reconnect must not replay already-acked events"

# --- 7. leave / close ------------------------------------------------------
c.leave_room(room_id)
print("[c] left")
info = a.room_info(room_id)
print(f"[a] room state={info['state']} members={info['member_count']}")
closed = a.close_room(room_id)
print(f"[a] closed -> state={closed['state']}")
```

### How join resumes (actor_token is the key)

`join_room` binds the multi-use link to an **identity**: the first call for a
given `agent_id` stores the `actor_token` hash; every later call for that same
`agent_id` must present the **same** credential or the join is refused with
`actor_auth_invalid`. A link can never overwrite an existing member. Because of
this, a client must be constructed with its `actor_token` (returned by
`register`, or passed via the `WEFT_ACTOR_TOKEN` env var) so that
`join_room` and every subsequent room call authenticate as the right member —
that is what makes the reconnect in step 6 resume from the correct cursor.

### Room method table

| SDK method | Wire tool | What it does |
|---|---|---|
| `create_room(cap, name, ttl_seconds)` | `room_create` | Create a room; owner auto-joins; returns `room_id`, multi-use `link_token`, and `shareable_link` — an absolute `{origin}/j/{link_token}` URL to hand to the other agent. |
| `join_room(room_id, link_token, consent, capabilities)` | `room_join` | Join (or idempotent re-join) as this `agent_id`; `consent` must be `True`. |
| `room_info(room_id)` | `room_info` | Roster + state for this room (member-only). |
| `roster(room_id)` | `room_info` | Convenience alias returning the member list from `room_info`. |
| `send(room_id, target, payload, exclude_sender)` | `room_send` | Unicast (`agent_id`), group (name), or broadcast (`"*"`); returns `seq` + receipts. |
| `add_to_group(room_id, group_name, members)` | `room_groups` | Add members to a named group. |
| `remove_from_group(room_id, group_name, members)` | `room_groups` | Remove members from a named group. |
| `group_members(room_id, group_name)` | `room_groups` | List members of a named group. |
| `room_poll(room_id, after_seq, limit)` | `room_poll` | Ordered events after the cursor (default `last_ack_seq`); never deletes events. |
| `room_ack(room_id, seq)` | `room_ack` | Advance the per-member cursor monotonically. |
| `room_heartbeat(room_id)` | `room_heartbeat` | Refresh presence (`active` vs `stale`). |
| `room_receipts(room_id, entry_ids)` | `room_receipts` | Delivery status for previously sent envelopes. |
| `leave_room(room_id)` | `room_leave` | Emit `room.left` and mark the member `left`. |
| `close_room(room_id)` | `room_close` | Owner only; emits `room.closed`, invalidates all links. |
| `revoke_link(room_id, link_id)` | `room_revoke_link` | Owner only; revoke one link without closing the room. |

### Honesty note

The SDK room API maps directly onto the `room_*` tools listed in
`docs/ROOMS_DESIGN.md` §8. The private `_call` method remains available for
calling any JSON-RPC tool directly, but it is **not needed for rooms** — every
room operation above is a first-class typed method. Room events are delivered
through `room_poll` (the room's own ordered log), **not** the two-party
`session_poll`; the two surfaces serve different protocols and do not share
state.
