# Orchestration workflow

How this project is built. CEO (Claude Code) directs; opencode orchestrators execute; LongCat-2.0
lanes fan out. This file is **self-improving** — §8 defines how it updates. Read §7 before
debugging anything; most of it has already been hit and diagnosed.

---

## 1. Roles

| Role | Who | Does | Never does |
| --- | --- | --- | --- |
| **CEO** | Claude Code | Sets goal, architecture decisions, wave briefs, review gates, independent verification, unblocks | Writes source code. Commits agent work. Runs line tasks. |
| **Orchestrator** | opencode + `deepseek-v4-flash`, one per worktree | Reads brief, dispatches lanes, wires shared files sequentially, commits per step, reports | Lets a lane touch shared files. Batches commits to the end. |
| **Lane** | `@longcat-2.0` subagent | One bounded task, exclusive file ownership, red-first, stops and reports | Commits. Touches a file it does not own. Implements past its scope. |

CEO writes `.md` only: briefs, specs, this file, `AGENTS.md`, roadmap.

---

## 2. The wave loop

Every unit of work is a **wave**. Same cycle each time.

1. **CEO writes the brief** (`scratchpad/wave_<x>_task.txt`) — context, deliverables, method,
   constraints, verification, report format. See §4.
2. **Design gate** (only when the change is risky — new security model, schema, or shared
   contract). One lane produces a doc, **doc only, no code**. CEO reviews and can reject.
   Cost: ~10 min. Catches design errors before N lanes build on them.
3. **Red contracts.** Parallel lanes, one file each, write *failing* integration tests that drive
   the outermost real surface. Lanes confirm RED, paste it, stop.
4. **Sequential wiring.** Orchestrator alone edits shared files (`core.py`, `server.py`,
   `__init__.py`). Parallel edits there silently clobber.
5. **Green one contract at a time**, full suite + smoke after each, **commit per step**.
6. **CEO verifies independently** — re-runs the suite, smoke, gate; diffs the contracts to confirm
   no assertion moved; checks claimed numbers.
7. **Docs in the same commit** as the code they describe.

Waves that are diagnosis-heavy (a flaky test, a race) **do not fan out** — one agent with full
context beats five with partial. Fan out for independent construction only.

---

## 3. Dispatch mechanics

### Resume the working session (default)
```bash
opencode run --session <session-id> --dir <worktree> "$(cat brief.txt)" > out.txt 2>&1
```
Run it backgrounded. **Redirect to a file — never pipe to `tail`**; the pipe holds the process open
after the loop exits and it hangs on teardown (§7.2).

### Second parallel orchestrator
```bash
git worktree add ../<name>-<branch> -b <branch>
opencode run --session <working-session> --fork --dir <new-worktree> -m opencode-go/deepseek-v4-flash "$(cat brief.txt)"
```
`--fork` inherits a **working model config and full project context**. A cold `opencode run`
without `--session` fails when the configured `small_model` provider is out of credits (§7.6).

Separate worktrees give separate working dirs **and separate git index state** — two orchestrators
cannot corrupt each other's staging. Merge branches when both land.

### Model pinning
- Subagents inherit the parent model unless the agent definition sets `model:`.
- Agent definitions: `~/.config/opencode/agents/<name>.md`, frontmatter `mode: subagent`,
  `model: LongCat/LongCat-2.0`.
- **Config is read at session start and does not hot-reload.** Changing it requires a restart.
- Project-scoped `opencode.json` only loads when the session is **rooted at that project**.

### Useful flags
`--dir` project root · `--session` resume · `--fork` branch off · `-m provider/model` ·
`--variant high` reasoning effort · `--format json` machine-readable events · `--title`.

---

## 4. Brief template

Nine blocks. Omitting any one has cost a run.

1. **Environment** — worktree path, `workdir` on every bash call, what never to touch.
2. **Verified state** — what CEO measured, so it does not re-derive or distrust it.
3. **What went wrong last run**, if anything, and whose fault. Naming CEO error keeps it honest.
4. **File ownership** — exclusive list per lane; shared files reserved to the orchestrator.
5. **Read first** — 3–5 files with a reason each.
6. **Task** — bounded, verifiable.
7. **Method** — ordering when it matters (design → red → wire). Say *why* an ordering is required.
8. **Constraints** — anti-weakening clause, dependency rules, backward compatibility.
9. **Verify + report** — exact commands; report separates verified from claimed; mandatory
   `NOT DONE:` field.

Rules that earn their place every time:
- *"Never weaken, skip, or delete an assertion to make a suite pass. If a test encodes a dead
  requirement, say so and propose the replacement invariant."*
- *"A negative test accompanies every enforcement claim."*
- *"Commit per step, explicit paths, never `git add -A`."*
- *"State explicitly what you could not finish."*

---

## 5. Lane design

- **Disjoint file ownership is the whole mechanism.** One lane, one file set, no overlap.
- Shared files (`core.py`, `server.py`, `__init__.py`, `pyproject.toml`, `AGENTS.md`) belong to the
  orchestrator alone.
- Integration tests drive the **outermost real surface** (MCP JSON-RPC dispatch), never a module's
  Python API. Testing the Python API is how five modules passed their own tests while shipping
  nothing (§7.4).
- Lanes stop at RED. Implementation is the orchestrator's, because it touches shared files.
- Parallel lane count is bounded by genuinely independent work, not ambition. 8 modules → 8 lanes.
  One race condition → 1 agent.

---

## 6. Verification and monitoring

### CEO verification gate — never accept a report at face value
```bash
python -B -m unittest discover -s tests          # exact count and result
python -B scripts/finalisma-smoke.py             # evidence_passed: true
python -B scripts/finalisma_performance_gate.py  # no regression
netstat -ano | grep <port>                       # no stray listeners
git --no-pager diff --stat <before> <after> -- <files-that-must-not-change>
```
Then: did any contract assertion move? Do the claimed numbers match? Is the `NOT DONE` list honest?

### Monitoring a run
Per-minute ticker reporting `procs / step / commits / changed / HEAD / last tool call`.
- `step` advancing = alive. Flat + **zero child processes** = stalled (§7.1).
- Flat + live children = long command, wait.
- `step` dropping to a low number = a subagent session started.
- `changed` and `commits` are the real progress signals; `step` is only liveness.

Log: `~/.local/share/opencode/log/opencode.log` — `message="exiting loop"` marks completion.
Transcripts and reports: `~/.local/share/opencode/opencode.db`, tables `message` / `part`,
joined on `session_id`. The report is readable there before the process exits.

---

## 7. Failure modes — already hit, already diagnosed

Status: **CONFIRMED** (reproduced) · **DISPROVEN** (tested, false — kept so nobody re-investigates)
· **OPEN**.

| # | Failure | Root cause | Fix | Status |
| --- | --- | --- | --- | --- |
| 7.1 | Tool call never returns after starting a server | A bash call waits for **every descendant holding the pipe**; a server never exits | Python driver owns the child via `subprocess.Popen`, teardown in `finally`, run under `timeout` | CONFIRMED ×3 |
| 7.2 | Process lingers after the loop exits | `opencode run ... \| tail` — `tail` waits for EOF | Redirect to a file, never pipe | CONFIRMED ×2 |
| 7.3 | 30+ min of work lost, unstaged | Commits batched to the end of a long run | Commit per step | CONFIRMED ×2 |
| 7.4 | Modules pass their own tests but ship nothing | Unit tests hit the Python API; nothing imported the module | Integration tests through the real MCP surface | CONFIRMED |
| 7.5 | Broken snapshot committed | `git add -A` against a mid-edit tree | Explicit paths only | CONFIRMED |
| 7.6 | New session returns empty, `finish=unknown`, 0 tokens | `small_model` routes to a credit-exhausted provider; touched at session setup | `--fork` an existing working session | CONFIRMED |
| 7.7 | `Start-Process -WindowStyle Hidden` + redirects hangs | — | — | **DISPROVEN** — tested both variants, returned in seconds. Real cause unknown; avoid the class (7.1) rather than debug it |
| 7.8 | Suite fails ~1-in-N, passes on re-run | `close()` only closed **idle** pooled connections; in-flight ones kept the SQLite file locked on Windows | Track live connections, close all | CONFIRMED — was a real production shutdown bug |
| 7.9 | Guidance outlives the code | Docs edited in a different commit from the code | Same commit, always | CONFIRMED |
| 7.10 | Perf gate red under machine load | Contention, not code | Do **not** re-baseline to force green; report honestly | OPEN |
| 7.11 | Run hangs AFTER completing and committing its work | Hang is a transport/pipe artifact at teardown, not lost work | Per-step commits make the hang harmless — nothing is lost; on resume, verify HEAD and continue | CONFIRMED ×4 |

---

## 8. Self-improvement protocol

This file is updated **at the end of every wave**, by the orchestrator, in the wave's final commit.

**Append when:**
- A run stalls, fails, or wastes >5 minutes → new §7 row with root cause and status.
- A CEO instruction proves wrong → record it as DISPROVEN with the test that killed it. Do not
  delete it; an unmarked dead theory gets re-investigated.
- A technique measurably works → add it to §2–§6 in one line. "Measurably" means it caught a
  defect or removed a failure, not that it felt good.
- A number changes (lane counts, timings) → update in place.

**Rules:**
- Every §7 row carries a status. `DISPROVEN` rows stay.
- No entry without evidence — a command, a commit hash, or an observed failure.
- Delete anything superseded by a better technique; keep the §7 history.
- Compression is an improvement. If two entries say the same thing, merge them.
- This file is prompt context for every run. Length is a real cost — cut ruthlessly.

**What "improvement" means here:** fewer stalled runs, fewer CEO corrections per wave, fewer
defects surviving to the verification gate. If a change to this file does not move one of those,
it does not belong.

### Wave log

| Wave | Lanes | Outcome | Lesson added |
| --- | --- | --- | --- |
| A–B | 16 | 8 modules + 8 product lanes | Disjoint ownership works at 8-wide |
| C | 5 | Modules mounted behind MCP surface | 7.4 — unit tests hid orphan modules |
| D | 0 | Host validation + YC honesty | Real host found a JS safe-integer bug no Python test could |
| E | 6 | Rooms: N-agent multi-use link | Design gate caught the one-use→multi-use security delta |
| F | 3+ | Cloud spine | 7.8 — chasing a flaky test found a production shutdown bug |
| G | 3 | Interop tiers 2–4 (parallel worktree) | Second orchestrator via `--fork`; worktrees remove contention |
