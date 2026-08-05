# Finalisma — agent rules (branch `isolated`)

Read `ORCHESTRATOR_BRIEF.md` in this directory before planning work or dispatching a lane.
It contains the full analysis, lane specs, prompt template, and verification gates.

## Worktree boundary

- This worktree is `C:\Users\Wasif\Documents\Multiplayer-AI-isolated`, branch `isolated`.
- `C:\Users\Wasif\Documents\Multiplayer-AI` is a **separate worktree** on `master` with its own
  uncommitted work. Never read from or write to it.
- If your shell cwd is `C:\Users\Wasif` (the opencode session root), every `bash` call must pass
  `workdir` = this worktree, and every read/write/edit must use an absolute path under it.
  Restarting opencode from inside this directory fixes that permanently.
- Do not commit from a lane. Do not run `git reset --hard`, `git checkout --`, or `git clean`.
- Never `git add -A`. Stage explicit paths. `git add -A` is what produced the broken snapshot
  described below.

## AGENT_HANDOVER.md is stale in four sections

`AGENT_HANDOVER.md` is otherwise authoritative. These four sections are **wrong** and will cause
damage if followed:

- **§2.1 / §2.2 — the design.** They describe **DOUBLE ENTRY** (Archivo + IBM Plex Mono, 37/63
  ledger rule, "no serif anywhere", `serifDeclarationsRemaining` must be 0). The live design is
  **FIELD NOTES** — see `site/styles.css` line 2 — built on **Fraunces, a serif, with italics**,
  plus Big Shoulders Display, oxblood cover bands, and ASSERT/PROVE colour roles. Fraunces is
  correct and deliberate. **Do not "fix" serif usage. Do not restore DOUBLE ENTRY.**
  `design-qa.md:5` is stale for the same reason.
- **§5 — subagents.** Describes Codex tools (`multi_agent_v1__spawn_agent`) that do not exist
  here. The mechanism in this environment is the `task` tool with
  `subagent_type: "longcat-2.0"`, defined in `~/.config/opencode/agents/longcat-2.0.md`.
  Subagents inherit the parent model unless the agent definition pins `model:`. Config is read at
  session start and does not hot-reload.
- **§9 — workspace.** Names the wrong worktree and references `apply_patch`, a Codex tool.

**§4.2 (traps) and §7 (security boundaries) are current — obey them.** In particular:
`minmax(0, 1fr)` never bare `1fr`; no horizontal padding on any element carrying the split; never
reveal with `clip-path` on an IntersectionObserver target; `overflow-x: clip` not `hidden`.

## Resolved — do not re-investigate

These were real defects and are all fixed. Listed so nobody spends a lane re-discovering them.

- `b4f3026` was committed with `git add -A` against a mid-edit tree and missed
  `site/proof-engine.js` (restored, `7c95cba`). `site/docs/managed-pilot.html` was an
  unreferenced draft superseded by `site/docs/pilot.html` and was deleted.
- The three `test_site.py` failures were genuine defects, not stale tests: the missing
  `[hidden] { display: none !important; }` progressive-enhancement rule, the colour-alone
  accessibility invariant, and dangling internal links. All fixed in code, assertions kept.
- `site/docs/pairing-ux.html` did not exist and was authored rather than the link being deleted.
- The credential-rotation defect (a rotated actor token still authenticated) is fixed.
- Two HIGH security findings are fixed: the `WebhookBridge` stored-hash-as-HMAC-key fallback now
  fails closed (`bridge.py:226`), and the SDK no longer leaks coordinator response bodies into
  exceptions (`client.py:269`).
- The five Wave-A modules are mounted behind the MCP surface (`fe4b2c5`) with integration
  contracts that drive real JSON-RPC dispatch, not the module APIs.
- A real MCP host (opencode 1.18.13) loaded finalisma from its own config and completed a full
  two-agent handoff (`96d76ba`, `docs/INTEROP_VALIDATION_2026-08-05.md`). The interop matrix now
  records 1 verified host.
- Wave E Rooms are implemented (`src/finalisma_mcp/room.py`): one multi-use link admits N agents
  up to a cap, with ordered event log, per-member cursors, addressing (unicast/group/broadcast),
  and 12 `finalisma_room_*` tools. Design: `docs/ROOMS_DESIGN.md`.

## The one open structural gap

The interop matrix previously recorded zero verified host integrations; it now records one
(OpenCode 1.18.13). The remaining gap is breadth: the stdio tier is host-verified, but the
Streamable-HTTP, bridge-adapter, and SDK tiers each still need a committed transcript before
they count as "supported" per docs/PRODUCT_ROADMAP.md §3. A negative result is valuable; a
simulated one is not.

## Test discipline

- TDD, red first. Write the failing test, run it, confirm it fails, then implement.
- **Never weaken, skip, or delete an assertion to make a suite pass.** If a test encodes a
  genuinely dead requirement, say so explicitly and propose the replacement invariant.
- No mocks for the SQLite layer. This project tests against real storage.
- Baseline for comparison: `docs/BASELINE_2026-08-05.md` (create it if absent — pin commit, test
  count, and exact failing test names before starting a wave).
- The published test count is now guarded automatically by
  `tests/test_site.py::TestCountSyncTests`, which discovers the live count with the same loader
  and pattern as `unittest discover -s tests` and fails if any instance in `docs/`, `site/**.html`
  or `site/llms.txt` disagrees. If you add or remove tests, that guard will tell you what to
  update. Do not hand-maintain the number.
- No performance figure may appear in `docs/` or `site/` that was not measured against the
  current harness. `docs/PERFORMANCE.md` holds the provenance. Dated audit documents keep their
  historical figures with a superseded-by note — annotate history, never rewrite it.

## Constraints that are product promises, not preferences

Python 3.11+, standard library only, SQLite state, zero runtime dependencies, no CDN, no global
installs, no package added just to run a check. The dependency-free claim is on the website.

## Reporting

Report truthfully — this repo's entire positioning is "evidence-backed", so a false completion
claim is a product bug. Never state a number you did not measure this session. Never report a
lane complete on the lane's own say-so; verify with a command and paste the output. Always state
what you could not finish.

## Long-running processes — read before starting a server

This environment has hung three times on this exact mistake. A `bash` tool call does not return
until **every** descendant holding the pipe has exited, so starting a server the normal way
blocks the tool call forever and the whole run stalls with no error.

Never do this:

```bash
python scripts/finalisma-mcp.py --transport http --port 18787 &   # BLOCKS the tool call
```

A `Start-Process` wrapper was tried and also stalled once, for reasons not reproduced in
isolation. Do not spend a run debugging shell process management — **avoid the whole problem
class instead.**

**Preferred: let a Python script own the child process.** One foreground command, deterministic
cleanup, no detachment, no orphan risk:

```python
import subprocess, json, sys
proc = subprocess.Popen(
    [sys.executable, "-B", "scripts/finalisma-mcp.py"],       # stdio transport
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
try:
    proc.stdin.write(json.dumps(request) + "\n"); proc.stdin.flush()
    line = proc.stdout.readline()                              # parse JSON-RPC reply
finally:
    proc.terminate()
    proc.wait(timeout=10)
```

Run it as `timeout 120 python -B scripts/<driver>.py`. The `finally` block guarantees teardown
even when an assertion fails, which a shell sequence does not.

This is also the more faithful test: **stdio is MCP's primary transport** — it is what Claude
Desktop, Claude Code and Cursor actually use — so driving the server over stdio validates the
path real hosts take, while an HTTP server on a port does not.

Rules that still apply: if you must bind a port, check it is free first; write scratch under
`AppData/Local/Temp/opencode/`, never into the repo; wrap every call in `timeout`; and never
leave a listener behind. Verify with `netstat` at the end of the step, not at the end of the run.

## Self-verification — verify by a different route than you built by

A claim checked the same way it was written is not checked. When you assert something works,
confirm it by an independent route:

- Wrote a guard? Feed it a value you know is wrong and prove it *fails*. A guard never seen red
  is not known to guard anything.
- Claim a module is reachable? Call it through the outermost real surface, not its Python API.
- Claim a doc is accurate? Grep the tree for contradicting instances rather than re-reading your
  own edit.
- Docstrings are claims. If a docstring says it covers X and Y, the code must cover X and Y —
  that exact mismatch shipped once already in `TestCountSyncTests`.

## Failure modes already seen in this project

1. **Blocking on a background process** — see above. Cost two stalled runs.
2. **Dying with work uncommitted** — two runs ended after 30+ minutes with everything unstaged.
   Commit each step as it completes. Never batch commits to the end of a long run.
3. **`git add -A`** — produced the broken `b4f3026` snapshot. Always stage explicit paths.
4. **Stale guidance outbliving the code** — `AGENT_HANDOVER.md` described a deleted design for
   weeks. When you change something this file or the handover describes, update it in the same
   commit.

## Skills

The user maintains a large skill library at `C:\Users\Wasif\.agents\skills\`. Skills are
instructions, not magic — load the ones relevant to the step you are on and follow them. Useful
here: `agent-orchestrator` (scan → match → orchestrate before fanning out lanes),
`test-driven-development`, `invariant-guard`, `anti-sycophancy` and `dos-verify-done-claims`
(before writing any completion report), `find-bugs` and `production-code-audit` (review),
`e2e-testing` and `debugging-toolkit` (host validation), `pitch-psychologist`,
`objection-preemptor` and `clarity-gate` (investor-facing copy). Say which you loaded.

## Verification commands

```powershell
python -B -m unittest discover -s tests
python -B scripts/finalisma-smoke.py          # expect evidence_passed: true
node --check site/app.js
node scripts/capture-site-qa.cjs              # expect consoleErrors: [] and all booleans true
python scripts/finalisma_performance_gate.py  # locked baseline; 59.314ms weighted median
```
