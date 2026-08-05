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

## Known-broken state on this branch

The initial commit `b4f3026` was made with `git add -A` against a mid-edit working tree and
missed two files that `site/index.html` links to:

- `site/proof-engine.js`
- `site/docs/managed-pilot.html`

Both exist in the master worktree; neither is on this branch. The site bundle has real 404s.
This is why `test_static_internal_content_links_resolve_inside_site_bundle` fails.

Three `test_site.py` failures predate this session's work. **None of them are stale tests.** All
three are genuine defects — a missing `[hidden] { display: none !important; }` rule that is
load-bearing for progressive enhancement, the colour-alone accessibility invariant, and the
dangling links above. Fix the code, not the assertions.

## Test discipline

- TDD, red first. Write the failing test, run it, confirm it fails, then implement.
- **Never weaken, skip, or delete an assertion to make a suite pass.** If a test encodes a
  genuinely dead requirement, say so explicitly and propose the replacement invariant.
- No mocks for the SQLite layer. This project tests against real storage.
- Baseline for comparison: `docs/BASELINE_2026-08-05.md` (create it if absent — pin commit, test
  count, and exact failing test names before starting a wave).
- The test count is published as a measured fact in `site/index.html`, `tests/test_site.py`,
  `docs/PERFORMANCE.md`, `docs/YC_APPLICATION.md`, `docs/YC_READINESS.md`, and two audit docs.
  It currently says 65 and is wrong. Fix it once, at the end of a wave, from one measured run.

## Constraints that are product promises, not preferences

Python 3.11+, standard library only, SQLite state, zero runtime dependencies, no CDN, no global
installs, no package added just to run a check. The dependency-free claim is on the website.

## Reporting

Report truthfully — this repo's entire positioning is "evidence-backed", so a false completion
claim is a product bug. Never state a number you did not measure this session. Never report a
lane complete on the lane's own say-so; verify with a command and paste the output. Always state
what you could not finish.

## Verification commands

```powershell
python -B -m unittest discover -s tests
python -B scripts/finalisma-smoke.py          # expect evidence_passed: true
node --check site/app.js
node scripts/capture-site-qa.cjs              # expect consoleErrors: [] and all booleans true
python scripts/finalisma_performance_gate.py  # locked baseline; 59.314ms weighted median
```
