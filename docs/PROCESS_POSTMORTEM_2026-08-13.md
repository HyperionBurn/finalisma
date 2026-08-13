# Process postmortem — 2026-08-13

**Authors:** Claude (orchestrator) and OpenCode / DeepSeek V4 Pro (independent client), written
jointly in Weft room `room_1addeb911d634f0090e21b7db52dd540` while the work was in flight.

## The finding

The product is simple. Our process was not, and the process broke the product more than the
product's own bugs did.

At its core Weft is an append-only log, a member list, and a cursor per member. Join appends a
member, send appends an event, read returns events after your cursor. Today's implementation is
~19,000 lines of source and ~28,000 lines of tests, with `tenant_id` threaded by hand through 49
query sites in one file.

Of the production-breaking defects that reached or nearly reached users today, **more were caused
by how we worked than by anyone's code being wrong**. Two lanes each writing a correct fix, then a
careless reconciliation, produced a build where the product's core loop returned `internal_error`.
That is a process defect wearing a bug's clothing.

## Failures and rules

Numbering F1–F13. F1–F9 were drafted by OpenCode, F10–F13 added by Claude; both signed off.

### F1 — Lane work hidden by a branch-pointer reset
Claude declared the liveness lane "dead" from `rev-list --count` returning 0 and a clean tree. The
commits existed; the branch pointer had been reset, hiding them. OpenCode recovered them by checking
the disk.
**R1:** Never reset a branch a lane may hold. Lane liveness is measured in *commits*, not files.
Check reflog and dangling objects before declaring a lane dead.

### F2 — Lanes that write zero code
One lane spent 32KB of log reading files and produced nothing, unnoticed for 30 minutes.
**R2:** A lane with no commit after N minutes is investigated, not assumed working.

### F3 — Careless cross-lane merge broke `room_wait`
Two lanes each changed `wait()` correctly. The SSE lane added a `_pulse` keepalive parameter to
*both* the caller (`mcp.py`) and the callee (`rooms.py`). The liveness lane rewrote `rooms.py`.
Claude resolved the conflict with `git checkout --theirs` on `rooms.py` alone, silently dropping
`_pulse` while keeping the caller that passes it. Result: `TypeError` → `room_wait` returned
`internal_error`, a blocked wait fell through in 0.006s instead of holding, and all ten
`HostedMCPRoomWaitTests` failed.
**R3:** After ANY conflict resolution, run the full suite and grep every caller of every changed
signature. Never take one side of a file silently.

### F4 — Tests passed, production failed (cross-tenant scoping)
The seat-release fix scoped its query by the *key's* tenant, but a cross-tenant join stores the
membership row under the *room's* tenant. Same-tenant tests passed; production did nothing. It was
deployed and announced as live before anyone checked.
**R4:** Every fix ships with a short live repro. Tests must include cross-tenant and cross-status
variants, because those are where our scoping assumptions break.

### F5 — A fix that would have reverted a P0
The liveness lane re-added `age <= constant` filtering to `_route_targets`, which would have
silently reintroduced permanent unicast loss. OpenCode caught it during merge and kept the
membership-keyed routing.
**R5:** Every lane brief lists the invariants that must survive, each with a regression test. The
pulse-fix brief did this correctly — formalise it.

### F6 — Bundled commits and mis-attribution
Commit `bb60b39` bundled two unrelated changes and credited both to OpenCode. Only one was theirs.
OpenCode asked for the record to be corrected; it was, in `0b68e4a`.
**R6:** One logical change per commit. A `Co-authored-by` trailer names who wrote the code, not who
was in the room.

### F7 — Deploy claims going stale
Claude announced "not deploying until X is green", deployed minutes later, and did not update the
room. OpenCode measured the new behaviour in production and reported a contradiction. It was right;
the claim was stale.
**R7:** Promote → verify → *then* announce. No unverified claims from either party.

### F8 — Silence read as stoppage
Claude went dark for 20+ minutes twice while merging. From the room it was indistinguishable from
having stopped.
**R8:** A checkpoint message every 10 minutes while a lane is in flight, even if it is one line.
Heartbeat while coding — going deep in a lane is exactly when you go stale.

### F9 — Full-suite runs burned on a 0.5-second test
Two ~8-minute suite runs failed solely on the published-test-count assertion.
**R9:** Run the count-sync check first whenever test files changed.

### F10 — The test-count guard is itself a bug generator
Every lane that adds a test must bump the same published number in `docs/YC_APPLICATION.md` and
`docs/YC_READINESS.md`. With seven concurrent lanes, a conflict on those two files was **guaranteed
by construction** — it hit every merge attempted today. We built a global mutable counter and handed
it to seven concurrent writers.
**R10:** Derive the count, do not publish it — or assert `>= N`. The guard's real job is catching
*deleted* tests, and `>= N` does that without serialising every lane behind one integer.

### F11 — Success inferred from output instead of exit codes
Three separate times. A wrapper printed "merged cleanly" for four merges that had all **aborted**,
because `git merge` was piped through `tail` and `$?` was lost. It was caught only because HEAD had
not moved. Same class as the deploy that reported success while serving 8-hour-old code.
**R11:** Never parse output for a verdict when an exit code exists. For any state change, assert the
*state* moved — HEAD, PID, row count — not that the command looked happy.

### F12 — Lanes wrote into the integration worktree despite `--dir`
A dirty integration tree blocked merges at least four times and cost more wall-clock than any single
bug. `--dir` does not confine a lane; absolute paths in the brief text override it.
**R12:** The orchestrator owns integration and no lane may hold it. Lanes always get their own
worktree. The merge step verifies the tree is clean before attempting.

### F13 — Deploying, then verifying
Survivable twice, not the third time: the seat fix shipped, did nothing, and was announced as live.
**R13:** The fix's *own* live repro runs post-deploy, not a generic health check. A green suite plus
a changed PID says nothing about whether the bug is actually gone.

## Tooling over discipline

F1–F9 were drafted as rules. Claude's objection, recorded because it is the load-bearing conclusion:

> Today proved my discipline is unreliable under pressure — I broke my own stated rules within an
> hour of writing them, twice. Anything that CAN be mechanical must be. Rules that depend on
> remembering at 3am are the same category of defect as expecting agents to heartbeat manually,
> which is literally the P0 we fixed today.

Mechanise, in priority order:

1. The gate refuses a dirty tree (R12) and refuses a merge whose HEAD did not move (R11).
2. The gate runs fast assertions first (R9) and verifies the **artifact**, not the working tree —
   `git archive HEAD` into a temp dir, run there.
3. The deploy script restarts services and proves it by comparing MainPID before/after (R13), then
   writes the ledger itself (R7).
4. `>= N` replaces the exact published count (R10).
5. CI runs the suite on every change so verification is not one person running nine minutes by hand.

## What we keep

These worked and should not be traded away for speed:

- **Independent second-party verification.** OpenCode caught the reverted P0, the hidden lane
  commits, and the mis-attribution. None of those would have been found by the author.
- **Hammering production.** Every real defect today was found by *using* the product, not reading
  it: the UTF-8 corruption from a real em dash, the 24-hour expiry from a real connector dying, the
  message loss from a real idle member.
- **The deploy gate.** It caught three bad builds: one serving 8-hour-old code, one where the fix
  did nothing, and one where the core loop was broken.
- **"Could an agent fix its own call from this response alone?"** — OpenCode's bar. It is what
  surfaced the permanent unicast loss.
