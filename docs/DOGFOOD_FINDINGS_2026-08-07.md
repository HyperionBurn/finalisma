# Dogfood findings — first real integration

> **Status note (2026-08-07, rebrand lane):** finding #1 below has been
> FIXED. The internal `finalisma_` tool prefix was dropped (item 9 of the
> rebrand), so `tools/list` now returns unprefixed names (`room_create`,
> `register_agent`, `room_poll`). The historical text below is kept as the
> dated record of the defect; it no longer describes current behaviour. All
> other findings remain open.

We coordinated our own multi-agent build using Finalisma: MCP server on `127.0.0.1:18787`,
a room for the build team, opencode agents as members. These are the things that cost a
competent, motivated integrator real time. Every one was hit for real, not imagined.

Ordered by how much damage each does to a first integration.

---

## 1. Tool names double-prefix in any host that namespaces — **58/58 tools affected**

opencode surfaces our tools as:

```
finalisma_room_create
finalisma_register_agent
finalisma_room_poll
```

MCP hosts namespace tools by **server name**. Ours are *also* individually prefixed
`finalisma_*`, so the prefix lands twice. This affects every tool, in every namespacing host —
so it is the first thing a new integrator sees, and it looks careless.

It also costs tokens on every single tool listing: 58 tools × ~10 redundant characters, in every
context window, forever.

**Fix:** drop the `finalisma_` prefix from the tool names themselves (`room_create`,
`register_agent`, `room_poll`). The server name already provides the namespace. If bare names
feel too generic for non-namespacing hosts, make the prefix configurable — but do not hard-code
a prefix that the protocol already supplies.

## 2. `register_agent` returns the credential you need, and nothing says so

The whole auth flow hinges on `actor_token`, returned inside the `register_agent` response
alongside status fields. Nothing in the tool description, the `initialize` instructions, or the
error messages says "this is where your credential comes from."

It took four failed attempts to open one room:

```
register_agent  → "Missing required argument 'team_id'"
org_create      → "Missing required argument 'org_name'"
room_create     → "Missing required argument 'owner_agent_id'"
room_create     → "Actor token is invalid"     ← passed an empty token
```

Each error is individually correct. The sequence is still pure guesswork. The server's
`initialize` instructions say *"Use register_agent before mutations"* — which is the
right hint and stops one sentence too early.

**Fix:** say in the `register_agent` description that it returns `actor_token` and that every
mutating call requires it. Better: when a mutation arrives without a token, name the tool that
issues one in the error.

## 3. No idempotent way to get an existing agent's credential

Re-registering an existing `agent_id` returns an error rather than the existing token. So a
restarted agent — or any second process for the same identity — has no way to obtain its own
credential. Our lanes get killed and restarted constantly; every restart is a dead end.

**Fix:** make `register_agent` idempotent for an unchanged identity, or add an explicit
`get_agent_credential`. Note `rotate_agent_credential` exists but rotating on every restart is
the wrong semantic — it invalidates the credential the still-running instance may hold.

## 4. `cap` counts the owner, so `cap=25` admits 24 joiners

Creating a room with `cap=25` and joining 25 agents fails the last one with `room_full`. The
owner occupies a slot. Defensible, undocumented, and surprising — the number you type is not the
number of agents that fit.

**Fix:** document it on the `cap` argument, or make `cap` mean joinable seats.

## 5. `poll` truncates at 100 by default and it reads as message loss

`poll` defaults to 100 events (max 200) and sets `has_more`. In a 200-event room a single
`after_seq=0` poll returns only the joins — so a broadcast at seq ~201 looks **missing**. During
scale testing this produced "20 of 20 agents did not receive the broadcast," which was false:
all 199 receipts were issued and delivery was correct.

An integrator who does not notice `has_more` concludes the product drops messages.

**Fix:** the default is fine, the discoverability is not. Say so on the `limit` argument, and
consider returning a top-level `truncated: true` that is harder to miss than `has_more`.

## 6. Event payloads nest one level deeper than they read

`room_send` stores the caller's payload nested beside routing metadata:

```json
{"payload": {"text": "..."}, "target_spec": "a2", "targets": ["a2"]}
```

So the caller's `{"text": ...}` is at `payload.payload.text`. Asserting `payload["text"]` — the
obvious guess — silently fails. This produced a false "unicast delivery is broken" in our own
proof script, when the send was working perfectly.

**Fix:** document the envelope shape in `room_send`/`room_poll`, or flatten the caller payload
and move routing metadata to a sibling key.

## 6b. Senders cannot tag a message kind — the highest-value gap for agent-to-agent work

The product's real job is letting one agent **hand its result to another**. The valuable payload
is the finished, human-readable output — what a person would read as the answer. Not reasoning:
chain-of-thought bloats every reader's context and costs tokens for no benefit.

But `room_send` has no semantic type. The event log's `kind` is system-owned (`room.created`,
`room.joined`, `room.message`), so *everything* substantive arrives as `room.message`. A
receiving agent that wants "results only, skip the liveness pings" has to parse payloads and
guess.

We worked around it by convention — `payload: {"kind": "status"|"result", "text": ...}` — but a
convention every integrator must invent independently is a missing feature. The consequence:
you cannot poll for "the results I have not yet consumed" without client-side filtering, which
is exactly the query an agent consuming another agent's output needs.

**Fix:** let the sender set a message kind (or subtype) as a first-class field, and let `poll`
filter on it. This is what turns the room from a status channel into an actual inter-agent bus.

## 6c. `create_task` deduplicates semantically across different idempotency keys

Passing a *different* `idempotency_key` still returned `created: false` with
`duplicate: {similarity: 0.708}` — matched on title similarity, not the key. The new task was
`null`; the only way forward was noticing the colliding id inside the `duplicate` object.

0.708 is a low bar. Two legitimately distinct tasks with similar titles will collide, and the
caller gets `task: null` with no obvious path.

**Fix:** an explicit `idempotency_key` should be authoritative — if the caller supplied a
distinct key, honour it. Keep similarity matching as a *warning*, not a refusal, or make the
threshold configurable.

## 6d. `create_task` rejects `priority` without naming the field

`priority: "high"` → `"Expected integer between 0 and 3"`. Correct, but it never says *which*
argument, and nothing in the schema hints that priority is an int scale rather than a label.

## 7. `org_create` requires `org_name` but `register_agent` only needs `team_id`

You can register an agent into a `team_id` that was never created. Two spellings of the same
concept with different requirements, so it is unclear whether org creation is a prerequisite.
(Empirically: it is not.)

**Fix:** align the vocabulary and state whether an org must exist before agents register into it.

---

## What worked, without qualification

- `initialize` → `tools/list` was clean, correct and instant. 58 tools, valid schemas.
- Reading `inputSchema` off `tools/list` gave exact required-argument lists and unblocked us
  immediately — the schemas are good, the prose around them is what is missing.
- Once the token was in hand, room create → join → send → poll worked first time, every time.
- **199 agents joined one link in 4.9s with a single identical event ordering**, and unicast
  payloads were correctly redacted for non-addressees. The hard part is right.

The pattern across all seven findings: **the mechanism is solid, the affordances are missing.**
Nothing here is architectural. All of it is naming, docs and error text — which is the cheapest
class of problem to fix and the most expensive to leave, because it is 100% of a new
integrator's first hour.

---

## Deploy-lane session findings

Hit while containerising `weft_cloud/service.py` and driving `prove-multiagent.py`.

1. **`target_spec: "*"` must be sent as the JSON string `"*"`, not a bare token.** In
   opencode, passing `target_spec: *` fails with "JSON Parse error: Unrecognized token '*'"
   and the call never reaches the server. The schema shows an object so the naive first
   attempt is a bare value; the correct form is `"*"`. Cost one failed call to learn.
2. **Broadcast recipients vary per send — the roster is a live set.** The same
   `room_send` with `target_spec: "*"` routed to `orch` on one call and `funnel-lane` on the
   next. The target set is resolved from currently-active roster members at send time. This
   is defensible but means an integrator cannot assume a stable broadcast audience, and a
   dead-silent room can be "broadcast to nobody" without error.
3. **Finding #6 bites the product's own acceptance script.** `prove-multiagent.py` asserted
   `event["payload"]["text"]` and failed "agent 4 did not receive the private message" when
   delivery was correct — exactly the `payload.payload.text` nesting #6 describes. Cost real
   debugging time to disprove a false delivery failure.
4. **`poll` and `event_log` return different event shapes.** `poll` returns a parsed
   `payload`; `event_log` returns raw DB rows (`payload_json` plus routing columns). Code
   that works against one shape KeyErrors on the other (`e["payload"]` vs `e["payload_json"]`).
   Either unify the shapes or document both on the endpoints.
5. **Non-member refusal is 404 `room_not_found`, not 403.** The documented contract is "403
   member_required OR 404 room_not_found" (404 is the no-oracle path). A proof script that
   demands 403 fails against a correctly secured service. Fine security posture; the duality
   is undocumented on the endpoint.
6. **Windows consoles (cp1252) crash on the "→" in step labels.** `prove-multiagent.py`
   died with `UnicodeEncodeError: 'charmap' codec can't encode '\u2192'`. Any script that
   prints non-ASCII should `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`.
   This is a general rule for every script and transcript in the repo.
7. **The suite is red before any deploy work.** On arrival: 512 tests, 9-11 failures + 19
   errors. The persistent set: 19 webapp errors + 3 webapp failures (all from in-progress
   uncommitted `src/weft_cloud/web/app.py`), the `frl_` prefix assertion (code emits
   `rm_`), and the count guard (docs still claim 396, live is 512). Plus 4 webapp tests are
   flaky (pass on some runs). A lane told to "keep the suite green" must know the baseline is
   already red or it will burn an hour re-discovering it.
8. **No container runtime exists on the build machine.** `docker`, `podman`, `buildah`,
   `nerdctl` all absent; Docker Desktop is not installed. `winget`/`choco` exist but Docker
   Desktop needs admin + a GUI engine start (WSL2), so the image build could not be verified.
   The deploy lane documented this in `docs/DEPLOY.md` rather than installing software
   without the founder's call.
9. **A `ThreadingHTTPServer` child can outlive its parent driver on Windows.** After
   several driver runs, `netstat` showed a leftover `python.exe` still listening on the
   service port even though each driver's `finally` called `terminate()`/`kill()`. Kill it
   by PID (`taskkill //PID <pid> //F`) and confirm with `netstat` at the end of the step —
   never trust teardown to have run.
10. **Room join link can be dead while the room id is live — and the agent cannot recover.**
    `email-lane` registered cleanly (`fst_actor_…`), then `room_join` against the brief's
    `room_509464fe…` + `rm_4LTn1s8…` pair failed with `invalid_link` ("Link is not valid for
    this room"). `pairing_preview` on the same token returned `pairing_not_found` ("Pairing
    link is invalid or has been revoked"). `room_info`/`room_poll` then failed with
    `member_required` — the room is fine, the agent is just not in it. There is **no tool to
    re-discover a valid join link** (no room-list-by-team, no owner-initiated re-issue reachable
    by a non-member), so an agent handed a stale link is permanently outside the room and must
    be re-invited by a human. Suggested: an error on `room_join` that distinguishes
    *revoked/expired link* from *link belongs to a different room*, plus a room-list surface a
    non-member can use to find the owner to re-issue.
