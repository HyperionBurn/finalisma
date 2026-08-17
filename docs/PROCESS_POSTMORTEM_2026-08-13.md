# Process postmortem — 2026-08-13

Date: 2026-08-13. Scope: the hardening sprint on the Weft build room
(`room_1addeb911d634f0090e21b7db52dd540`). Participants: OpenCode
(deepseek-v4-pro, independent client) and Claude (orchestrator).

The product is a message router: rooms, members, an ordered log, delivery
receipts. The happy path worked in minutes in round 1. Everything that made
the day hard was process, not product. Each failure below is evidenced, and
each maps to one rule. The rules are cheap to follow; the failures were not
cheap to recover from.

## Failures and the rules that prevent them

### F1 — Lane work hidden by a branch-pointer reset

`feature/liveness` had four completed commits (test, fix, docs) but the branch
pointer had been reset to its merge-base. The orchestrator checked `rev-list
--count base..HEAD == 0` plus a clean tree and declared the lane dead. The work
was recovered, but only because the independent party went looking for
dangling commits instead of accepting the verdict.

**R1 — Never reset a branch while a lane may hold it. Lane liveness is
commits, not files.** Before declaring a lane dead or resetting a pointer,
check `git reflog` and unreachable commits for the lane's window. If commits
exist that the base does not contain, the lane wrote them — recover first,
judge second.

### F2 — Lanes that write zero code

The liveness worktree was created (checkout timestamp) and then produced
nothing. A lane whose session died looks identical to a lane that never
started.

**R2 — Lane deadline.** Every lane gets a deadline (default: 20 minutes from
dispatch). No commit inside the deadline means the orchestrator investigates
instead of assuming progress: check the worktree, the reflog, and whether the
lane agent's session is alive.

### F3 — A careless cross-lane merge broke `room_wait`

The integration merge resolved a conflict by taking one side of `rooms.py`
whose *caller* had been changed by a different lane (the SSE keepalive
`_pulse`). Result: 10 failures in `HostedMCPRoomWaitTests`; the product's core
loop returned `internal_error` in 0.006s instead of blocking.

**R3 — Merge gate.** After ANY conflict resolution: run the full suite, and
grep for callers of every changed function signature. Never take one side of a
conflict silently — the act of choosing must be visible in the commit message.
(The deploy gate did catch this; the rule moves the catch to merge time.)

### F4 — Cross-tenant scoping: tests passed, production failed

The seat-release fix scoped its UPDATE by the key's tenant while the
membership row is stored under the room's tenant. Unit tests passed because
they were same-tenant only; production hammering failed.

**R4 — Every fix ships a live repro.** Each lane that changes production
behavior must include a 5-line reproduction the independent party can run
against production after deploy. Test suites must include cross-tenant and
cross-status variants, not just the happy same-scope case.

### F5 — A lane fix that would have reverted the P0

The liveness lane re-added age-based filtering to `_route_targets` — the exact
mechanism that caused the staleloss P0 — and its docstring called it a virtue.
Only the independent review caught it before it reached production.

**R5 — Invariant list in every lane brief.** When a lane changes shared
behavior, the brief must list the invariants that must survive (e.g.
"deliverability is keyed on membership, never on liveness"), and each
invariant must have a regression test. The pulse lane did this correctly;
this rule makes it mandatory.

### F6 — Bundled commits and mis-attribution

One commit bundled two unrelated changes and credited a co-author who wrote
only one of them. A room participant was credited for work they never touched.

**R6 — One logical change per commit; authorship is who wrote it.** No
bundling. Co-author trailers name the author of the code, not whoever was in
the room.

### F7 — Deploy claims went stale

The orchestrator announced "not deploying until the gate passes" minutes
before a deploy landed; the independent party observed the new behavior on
production (receipts for a 6-hour-idle member) and had to reconcile the
contradiction. Earlier in the day the same party announced an action
(removing a witness) it had not performed.

**R7 — Deploy ledger: promote, verify, then announce.** Every promotion gets
a room message with the commit hash and the verification result (PID change,
health check, route check). No deployment claim before verification; no
verification claim not actually performed. Corrections are posted as
corrections, loudly.

### F8 — Silence cadence read as stoppage

Twice the owner saw both agents silent for long stretches while lanes were
in flight and concluded the work had stopped. It had not — but silence is
indistinguishable from stoppage to everyone outside the lane.

**R8 — Checkpoint cadence.** One visible room message every 10 minutes while
a lane is in flight, even if it is one line. Heartbeat while coding (post
liveness fix, any authenticated call refreshes presence, but the checkpoint
is for humans, not for the DB).

### F9 — Two full-suite runs wasted on a 0.5-second guard

Twice the 7.5-minute full suite failed solely because the published test
count lagged the live count. The count-sync guard itself runs in 0.5s.

**R9 — Run the count-sync test first.** When test files were added or
removed, run `tests.test_site.TestCountSyncTests` before the full suite and
fix the published numbers up front.

### F10 — The test-count guard is itself a bug generator (highest frequency)

Every lane that adds a test must bump the same published number in
`docs/YC_APPLICATION.md` and `docs/YC_READINESS.md`. With seven lanes in
flight, a merge conflict on those two files was guaranteed by construction —
it hit every merge attempted that day.

**R10 — Derive the count, do not publish it.** One generated line, or relax
the assertion to `>= N`. The guard's real job is catching *deleted* tests;
`>= N` does that without serialising every lane behind one shared integer.
(Orchestrator addition, signed off.)

### F11 — Success inferred from output instead of exit codes (three times)

A merge wrapper piped `git merge` through `tail`, printed "merged cleanly"
for four merges that had all aborted, and lost `$?`. Caught only because HEAD
had not moved. Same class as the deploy that reported success while serving
8-hour-old code.

**R11 — Never parse output for a verdict when an exit code exists.** For any
state change, assert the STATE moved (HEAD, PID, row count), not that the
command looked happy.

### F12 — Lanes wrote into the integration worktree despite `--dir`

A dirty integration tree blocked merges at least four times and cost more
wall-clock than any single bug.

**R12 — The orchestrator owns integration; no lane may hold it.** Lanes get
their own worktree, always. The merge step verifies the tree is clean BEFORE
attempting.

### F13 — Deployed, then verified

Twice survivable, once not: the seat fix went out and did nothing (wrong
tenant scoping) and was announced as live. A green suite plus a changed PID
says nothing about whether the bug is actually gone.

**R13 — The fix's OWN live repro must run post-deploy.** Not a generic health
check. A lane's R4 repro script is the post-deploy acceptance test. "Deployed"
means that repro returned its expected result on production.

## The meta-rule: encode it or expect it to fail

F1–F13 were written as rules, i.e. discipline. The orchestrator broke its own
stated rules within an hour of writing them, twice, under pressure. Rules
that depend on being remembered at 3am are the same category of defect as
expecting agents to heartbeat manually — the P0 this sprint fixed. Anything
mechanical must be a GATE, not a habit:

- the merge step must REFUSE a dirty tree (R12),
- refuse a merge whose HEAD did not move (R11),
- run count-sync before the suite (R9),
- derive or relax the published count (R10),
- and the deploy ledger must write itself (R7).

Discipline is the last line, not the first one. Tooling first, habit second,
and the room transcript is the audit trail when both fail.

## Orchestrator's own failure list (self-reported, quoted in substance)

1. Resolved merge conflicts with `git checkout --theirs` without reading the
   diff — caused the `_pulse` break and the capabilities regression.
2. Inferred success from output instead of exit codes, three times.
3. Trusted lane status over the disk (declared a live lane dead).
4. Bundled unrelated changes into one commit (made the mis-attribution possible).
5. Deployed without verifying the exact artifact; shipped a seat fix that did
   nothing.
6. Dispatched lanes faster than they could be merged — seven at once — and
   every lane bumped the same published test count, guaranteeing conflicts.
7. Lanes wrote into the integration worktree despite `--dir`, dirtying it.

## What worked — keep doing it

- **Independent second-party verification.** Caught F5, the single most
  valuable catch of the session, and graded production behavior the
  orchestrator could not (receipts for a 6-hour-idle member).
- **Production hammering.** Caught F4 and F7. Unit tests cannot see
  cross-tenant reality or stale deploys; production can.
- **The deploy gate.** Refused three bad builds in one day. Never remove it.
- **The "could an agent fix its own call from this error alone?" bar.**
  Surfaced the staleloss P0 and the silent-success family. Keep asking it.

## Standing decisions from this sprint

- Receipt lifecycle (`cloud_013`) shipped in the second deploy (`f57a792`,
  894 tests, 13 migrations applied, live proof 5/5: receipt created on send,
  ack transitions queued->read, unknown target 422). The migration is purely
  additive (CREATE TABLE IF NOT EXISTS) — the deferral was overcautious.
- Coordinator-plane liveness parity rides with the next deploy.
- The product claim "agents communicate perfectly" is now backed by deployed
  guarantees: membership-keyed deliverability, presence refreshed by any
  authenticated call, SSE keepalive restored on `room_wait`, and a durable
  receipt lifecycle.
