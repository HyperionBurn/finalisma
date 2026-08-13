# WEFT — ORCHESTRATOR BRIEF

**To:** the primary opencode agent (deepseek-v4-flash) running the Weft multi-lane build
**Branch:** `isolated` · **Worktree:** `C:\Users\Wasif\Documents\Multiplayer-AI-isolated`
**Status at brief time:** Wave A (8 LongCat-2.0 lanes) dispatched 12:19, in flight. 180 tests, 6 failures + 14 errors.

Read PART 0 and PART 1 before you touch anything or dispatch another lane. PART 1 contains
five facts the repository itself asserts that are **false**. If you or a lane trusts them, you
will destroy correct work.

---

## PART 0 — OPERATING RULES

### 0.1 Your shell is not where you think it is

Your opencode session is rooted at `C:\Users\Wasif`, **not** the worktree. This is the single
biggest source of silent failure in this session. Therefore:

- Every `bash` call **must** pass `workdir: "C:\\Users\\Wasif\\Documents\\Multiplayer-AI-isolated"`.
- Every `read`/`write`/`edit` **must** use an absolute path under that worktree.
- Never write to `C:\Users\Wasif\Documents\Multiplayer-AI` (master). It is a separate worktree
  with its own uncommitted work. Touching it breaks the isolation guarantee the user asked for.
- Restarting opencode **from inside the worktree** fixes this permanently. Do that at the next
  natural break. Until then, treat the `workdir` param as mandatory in every single lane prompt.

### 0.2 Truthful reporting is a hard requirement

`AGENT_HANDOVER.md` §10 and `docs/AI_READABILITY.md` make this a product invariant, not a
style preference. The repo's entire positioning is "evidence-backed." A false completion claim
in this repo is a product bug.

- Never report a lane "done" because the lane said so. Verify with a command and paste output.
- Never write a number into a doc or the website that you did not measure this session.
- If a lane failed, say which one, what error, and what you did about it.
- Load the `anti-sycophancy` and `dos-verify-done-claims` skills before you write any
  completion summary.

### 0.3 Commit discipline

Lanes are correctly told not to commit. **You** commit, one commit per lane, after that lane's
tests pass in isolation. Never `git add -A` — it is exactly what created the broken snapshot in
PART 1.5. Stage explicit paths.

```bash
git -C C:/Users/Wasif/Documents/Multiplayer-AI-isolated add src/weft_mcp/roster.py tests/test_roster.py
```

Never run `git reset --hard`, `git checkout --`, or `git clean` in either worktree.

---

## PART 1 — GROUND TRUTH CORRECTIONS

These are verified against the working tree. `AGENT_HANDOVER.md` is the first thing every agent
reads, and on five points it is now wrong.

### 1.1 The design section describes a deleted design — CRITICAL

`AGENT_HANDOVER.md` §2.1 and §2.2 describe **DOUBLE ENTRY**: a 37/63 ledger rule at
`--split: 37%`, seven folios, Archivo + IBM Plex Mono, "no serif anywhere,"
`serifDeclarationsRemaining` must stay 0, red-ink rules, `.ledger-ground` fixed backdrop.

`site/styles.css` line 2 says the live design is **FIELD NOTES** — a magazine feature with
oxblood cover bands, **Fraunces (a serif, with italics) + Big Shoulders Display**, and
ASSERT/PROVE colour roles. Line 12 states it explicitly replaced DOUBLE ENTRY, and gives the
reasons (monospace body copy read as terminal output, label/value tables read as spreadsheet).

`site/styles.css` even points back at "AGENT_HANDOVER.md §2.1" for the rationale — but §2.1 now
documents the design it replaced. `design-qa.md:5` also still describes DOUBLE ENTRY as the
source visual target.

**Consequence:** any lane assigned to the site will read §2.2, see "no serif anywhere," find
Fraunces, and "fix" it by deleting the current design. That is a catastrophic false positive.

**Action — do this before dispatching any site lane:** rewrite `AGENT_HANDOVER.md` §2.1/§2.2 to
document FIELD NOTES, with a short "superseded designs" note listing control-room broadsheet →
DOUBLE ENTRY → FIELD NOTES so nobody restores any of them. Update `design-qa.md:5`. Keep the
§4.2 trap list — those traps (`minmax(0,1fr)`, no padding on split grids, `clip-path` vs
IntersectionObserver, `overflow-x: clip`) are still real and still load-bearing.

### 1.2 The subagent rules describe a different runtime

§5 tells agents to use `multi_agent_v1__spawn_agent` / `multi_agent_v1__wait_agent` with
`agent_type: "default"`. Those are **Codex** tools. They do not exist in opencode. The correct
mechanism in this session is the `task` tool with `subagent_type: "longcat-2.0"`, backed by
`~/.config/opencode/agents/longcat-2.0.md`.

**Action:** rewrite §5 to describe the opencode mechanism, and record the model-pinning lesson
(subagents inherit the parent model unless the agent definition sets `model:`; config is read at
session start and does not hot-reload; project-scoped config only loads when the session is
rooted at that project).

### 1.3 The workspace rule points at the wrong worktree

§9 says "Work only inside `C:\Users\Wasif\Documents\Multiplayer-AI`." For this session the
correct boundary is the isolated worktree. §9 also says "use `apply_patch`" — a Codex tool.

**Action:** rewrite §9 for the current worktree and toolset.

### 1.4 The "65 tests" fact is now false in six places

`65` is stated as a **measured fact** — the handover §6 explicitly warns that if you change the
test count you must update every place that states it. The count is now **180**.

Currently asserting 65:
- `docs/PERFORMANCE.md:17`
- `docs/WEBSITE_COMPLETION_AUDIT_2026-07-31.md:28`
- `docs/YC_APPLICATION.md:32`
- `docs/YC_READINESS.md:23`
- `docs/AUTORESEARCH_WEBSITE_LAUNCH_2026-07-30.md:102`
- `site/index.html` microcopy + folio total, and the assertion in `tests/test_site.py`

**Action:** do **not** update these yet — the count is still moving while Wave A lands. Add this
to the integration checklist and do it once, at the end, from a single measured run. Then make
it a single source of truth: put the number in one place and have the others reference it, or
add a test that asserts the site copy matches the live count. Right now it is hand-copied into
six files, which is why it drifted.

### 1.5 The `isolated` branch was cut from a broken snapshot — CRITICAL

At 11:51 the initial commit `b4f3026` was made with `git add -A` against master's **mid-edit**
working tree. Two files that `site/index.html` links to were not captured:

| File | In master working tree | In `isolated` |
|---|---|---|
| `site/proof-engine.js` | 8.1K, present | **missing** |
| `site/docs/managed-pilot.html` | 4.5K, present | **missing** |

`site/index.html` on `isolated` references both. This is why
`test_static_internal_content_links_resolve_inside_site_bundle` fails. **It is not a stale test.
It is a genuinely broken site bundle** — a real 404 for a real user.

The other two site failures are the same class of problem:
- `test_progressive_enhancement_and_gated_story_cta` — asserts `[hidden] { display: none !important; }`
  exists in `styles.css`; the FIELD NOTES rewrite dropped it. That rule is **load-bearing for
  progressive enhancement** (without it, JS-gated content flashes visible before hydration). This
  is a real regression, not a stale assertion.
- `test_unposted_entries_never_rely_on_colour_alone` — the accessibility invariant from §2.1
  (every red row carries a word for its state). FIELD NOTES changed the colour system to
  ASSERT/PROVE; the invariant needs re-expressing against the new design, not deleting.

**Do not let a lane "fix" these by weakening the assertions.** All three are real defects.
Fix the code, keep the tests. If a test genuinely encodes a dead design detail, the lane must
say so explicitly and propose the replacement invariant — never silently relax it.

---

## PART 2 — STOP-THE-LINE: DO THESE BEFORE WAVE B

Do not dispatch Wave B until PART 2 is complete. You are about to add six more lanes on top of a
foundation with a broken bundle and a lying handover doc.

**S1. Repair the branch snapshot.**
Copy `site/proof-engine.js` and `site/docs/managed-pilot.html` from master into the worktree,
read both, confirm they are the versions `index.html` expects, then commit them as
`fix: restore site files missing from the initial snapshot`. If they are half-finished, remove
the references from `index.html` instead — but decide deliberately and say which you did.

**S2. Rewrite `AGENT_HANDOVER.md` §2.1, §2.2, §5, §9** per PART 1. This unblocks every site lane.
Also fix `design-qa.md:5`.

**S3. Pin and publish the baseline.** Write `docs/BASELINE_2026-08-05.md` containing: the
commit, the exact test count, the exact failing test names, and which failures are inherited vs
new. Every lane report is measured against this file. Without it you cannot tell Wave A breakage
from inherited breakage — and right now you cannot.

**S4. Triage the 14 errors.** They are almost certainly red-phase `ImportError` (tests written,
module not yet created) plus real bugs. Classify each: `RED-PHASE` (expected, lane still
working) vs `DEFECT` (lane finished and it is broken). Only the second kind is actionable.

Known real ones already visible:
- `test_deliver_emits_signed_event` — HMAC signature mismatch in `bridge`. Canonicalisation
  disagreement between test and impl. Real bug; must be settled by spec, not by copying the
  observed hash into the test.
- `test_rotate_credential` — `AuthError not raised` after rotation. **Security defect.** A stale
  actor token still authenticates after rotation. This contradicts §7 of the handover and
  `docs/SECURITY_GATES.md`. Highest severity item currently in the tree.
- `test_concurrent_http_clients_session_send_poll_ack` — `cursor_head == 0` expected 20. Session
  cursor not advancing under concurrent HTTP clients. Contradicts the durability claims.

**S5. Wire Wave A modules in.** `tenancy.py`, `roster.py`, `outbox.py`, `bridge.py`,
`weft_sdk/` are currently orphans — nothing in `core.py` or `server.py` imports them. Until
you mount them they are dead code that passes its own tests and ships nothing. This is your job,
not a lane's (correctly scoped that way). Budget real time for it.

---

## PART 3 — WAVE A INTEGRATION PROTOCOL

For each of A1–A8, in this order, one lane at a time:

1. **Read the lane's diff.** `git -C <worktree> status --porcelain` then read each new file. Do
   not accept a lane summary as evidence.
2. **Run that lane's tests alone.** `python -B -m unittest tests.test_roster -v`
3. **Run the full suite.** Confirm the delta vs `docs/BASELINE_2026-08-05.md` is only that lane's
   tests going green. Any other movement is a cross-lane collision — investigate before committing.
4. **Mount it.** Wire the module into `core.py`/`server.py` and add one integration test that
   exercises it through the real MCP surface, not the module API. A module with only unit tests
   is not integrated.
5. **Check the invariants.** Run `python -B scripts/weft-smoke.py` and require
   `evidence_passed: true`. Run
   `python -B scripts/weft_performance_gate.py --baseline .omx/goals/performance/single-node-coordinator-envelope/baseline.json`.
   The current reference artifact is 72.221ms; the earlier 59.314ms weighted median is
   historical evidence, not the current target. `tenancy` and `roster` add per-call work
   on hot paths and are the most likely regressors. If the gate regresses, that is a
   blocker, not a footnote.
6. **Commit** with scoped paths and a message naming the lane.

**Specific things to check per lane:**

| Lane | What to verify hardest |
|---|---|
| A1 arch | 455-line doc — check every claimed repo artifact actually exists. Architecture docs are where hallucinated file paths hide. |
| A2 protocol | v2 must be **additive**; a 1.0 envelope must still validate. Write the back-compat test yourself. |
| A3 tenancy | The rotation defect (S4). Cross-tenant isolation must be tested with a **negative** test — tenant B provably cannot read tenant A. |
| A4 roster | Performance gate. Routing is on the hot path measured by the locked benchmark. |
| A5 outbox | Crash-recovery: kill mid-delivery, restart, assert exactly-once. Retry logic that is only unit-tested is not durable. |
| A6 server-hub | The cursor bug (S4). Concurrency tests must actually run concurrently — check for `threading`, not sequential loops. |
| A7 bridge | The HMAC bug (S4). Also: the bridge is the "works for any host" claim — verify one-use enforcement and actor binding have negative tests. |
| A8 sdk | **A8 skipped the red phase.** It wrote 21.8K of `client.py` before any test file. Review that code as untested-until-proven, and require tests that were not written to match the implementation. |

---

## PART 4 — WAVE B (REVISED)

The original Wave B was perf / site-tests / site-nexus / telemetry / YC / GTM. Revise it. Here
are eight lanes with disjoint ownership, ordered by value.

**B1 — SITE-TRUTH** (owns `tests/test_site.py`, `site/index.html`, `site/styles.css`)
Fix the three real defects from PART 1.5. Restore the `[hidden]` rule. Re-express the
colour-alone accessibility invariant against FIELD NOTES ASSERT/PROVE. Resolve all internal
links. Then re-pin the test-count fact. **Explicitly forbid weakening any assertion.**

**B2 — ACTIVATION-INSTRUMENTATION** (owns a new `src/weft_mcp/metrics_activation.py`)
`docs/YC_READINESS.md` names eight metrics that must exist before any fundraising claim. None are
instrumented. Ship the activation funnel first: `link_created → link_previewed → link_accepted →
first_task_claimed → first_evidence_verified`, with **time-to-first-verified-handoff** as the
headline metric. Local-first, no external analytics vendor, no PII. This single lane converts
"we think it's fast" into the number the YC application needs.

**B3 — QUICKSTART-TTFV** (owns `README.md` quickstart, `site/docs/quickstart.html`, `examples/`)
The activation event is "two agents completed an evidence-gated handoff," not "installed the
MCP." Get the documented path from clone to first verified handoff under 5 minutes, and
**measure it with a stopwatch on a clean machine**. Every step that fails or confuses gets
recorded in `docs/PAIRING_UX.md`.

**B4 — HOST-INTEROP-MATRIX** (owns `research/interop-matrix.json`, `site/docs/compatibility.html`)
Handover §8.3 has been open the whole time: nobody has installed this in two real MCP hosts. This
is the highest-value unknown in the entire project — the core claim "works with any MCP host" is
**unvalidated**. Test against Claude Desktop, Claude Code, Cursor, Zed, Continue. Record exact
config UI, first-pairing time, failure modes, reconnect behaviour. A negative result here is more
valuable than any amount of new code.

**B5 — A11Y-AND-LIGHTHOUSE** (owns `scripts/capture-site-qa.cjs`, a11y fixes in `site/`)
The harness checks 13 bespoke booleans but has never run axe or Lighthouse. Add both to the
capture script. Target: zero axe violations, Lighthouse ≥95 on all four categories. Keyboard-only
pass and a real screen-reader pass on the reconciliation spread — it is the most interactive and
least tested surface.

**B6 — CONVERSION-DESTINATION** (owns a new `site/docs/pilot.html` + CTA wiring)
Handover §8.6: "add a real conversion destination. Do not invent an email capture or customer
traction." The site currently demonstrates and then dead-ends. Add one honest destination —
"apply to be a design partner," founder email, concierge onboarding offer. **No fake logos, no
fake counts, no invented testimonials.** Note `site/docs/managed-pilot.html` already exists in
master and may be a started version of exactly this — read it first.

**B7 — SECURITY-REVIEW** (read-only; owns `docs/SECURITY_REVIEW_2026-08-05.md`)
Wave A added tenancy, bridges, HTTP surface, and an SDK — the attack surface roughly doubled in
one afternoon, written by eight agents that could not see each other's code. Adversarial review
against `docs/SECURITY_GATES.md`. Priority: the rotation defect, bridge HMAC and replay, tenant
isolation, token handling in the SDK, and whether any new path logs a secret. Read-only, reports
findings ranked by severity.

**B8 — PERF-GATE-DEFENCE** (owns `scripts/weft_performance_gate.py`, `docs/PERFORMANCE.md`)
Re-run the locked gate after integration. The 95.31% improvement is a historical result
preserved in the YC application and baseline history; current claims must use the
re-baselined artifact and the latest controlled-host run. Extend the benchmark to cover
the new roster/routing and tenancy paths so any published number keeps meaning something.

---

## PART 5 — WAVE C: YC AND PRODUCT

Do not run Wave C until B2, B3 and B4 have produced real numbers. Everything here depends on
measured facts, and fabricating them is the one unrecoverable mistake in a YC application.

**C1 — YC-APPLICATION** (owns `docs/YC_APPLICATION.md`)
Four `[FILL]` placeholders remain, all in "Who is using it?" — design-partner count, workflow,
median time to first handoff, four-week repeat rate. The doc's own instruction is correct: until
those are real, say "we are recruiting design partners," **not** "teams use Weft."

Rewrite everything that *can* be made true now from B2/B3/B4 output. Strengthen these answers:
- *What is working today* — replace "65 passing tests" with the measured count, and add the
  interop matrix result from B4. A specific "verified against N of M hosts, here are the
  failures" is far more credible than a round test count.
- *Why now* — currently a category assertion. Ground it in the B4 matrix: multi-host is now the
  default and the handoff is manual, and here is the evidence.
- *What is the insight* — the strongest paragraph in the document. "Agent collaboration is a
  distributed-systems problem with a human consent boundary" is a genuine, defensible thesis.
  Lead with it. Everything else supports it.
- *What is not built yet* — keep it. Explicit scoping of what is not production reads as
  competence, not weakness. Update it as Wave A lands.

Skills: `pitch-psychologist`, `product-marketing`, `objection-preemptor`, `clarity-gate`.

**C2 — FOUNDER-NARRATIVE**
The demo video exists (42s, reproducible from a real coordinator run — `docs/DEMO_VIDEO.md`).
That is a genuine asset; most applications do not have one. What is missing is the founder video
script and the one-line answer to "why you." Do not fabricate biography.

**C3 — PRICING** (owns `docs/PRICING.md`)
`YC_READINESS.md` has a three-tier hypothesis (free / team / business) that has never been tested
against a human. B3's concierge onboarding is the natural place to test it. Skills:
`pricing-strategy`, `price-psychology-strategist`.

**C4 — DESIGN-PARTNER-PLAYBOOK** (owns `docs/GTM_PLAYBOOK.md`)
The 30-day experiment in `YC_READINESS.md` is well-specified: 10 teams, concierge onboarding for
their first three handoffs, charge at least three, kill the wedge if they do not return. It has
no execution artifacts. Produce: the outreach message, the qualifying questions, the concierge
session script, and the kill criteria written down **in advance** — the whole point of the kill
criterion is that it is written before you are emotionally attached to the result. Skills:
`launch-strategy`, `growth-engine`, `cold-email`, `customer-research`.

---

## PART 6 — SKILL REGISTRY

All verified present in `C:\Users\Wasif\.agents\skills\`.

**Load for yourself, every session:**
| Skill | Why |
|---|---|
| `agent-orchestrator` | You already use it. Keep the scan→match→orchestrate discipline. |
| `anti-sycophancy` | Eight lanes will report success. Some will be wrong. |
| `dos-verify-done-claims` | Turns "lane says done" into "verified done." |
| `full-output-enforcement` | Stops truncated lane reports passing as complete. |
| `closed-loop-delivery` | Enforces the verify-then-report loop. |
| `invariant-guard` | The repo is built on named invariants; this protects them. |

**Per-lane assignment — put the skill name in the lane prompt:**

| Lane | Skills |
|---|---|
| A1 arch | `architecture-decision-records`, `docs-architect`, `c4-architecture-c4-architecture` |
| A2 protocol | `api-design-principles`, `openapi-spec-generation`, `deprecation-and-migration` |
| A3 tenancy | `security-and-hardening`, `auth-implementation-patterns`, `privacy-by-design`, `gdpr-data-handling` |
| A4 roster | `python-performance-optimization`, `performance-profiling` |
| A5 outbox | `error-handling-patterns`, `event-sourcing-architect`, `distributed-tracing` |
| A6 server-hub | `api-security-best-practices`, `observability-and-instrumentation` |
| A7 bridge | `api-security-testing`, `frontend-api-integration-patterns` |
| A8 sdk | `api-sdk-generator`, `api-documentation`, `python-packaging`, `test-driven-development` |
| B1 site-truth | `design-taste-frontend`, `frontend-dev-guidelines`, `accesslint-audit` |
| B2 instrumentation | `analytics-product`, `observability-and-instrumentation`, `kpi-dashboard-design` |
| B3 quickstart | `developer-onboarding`, `api-onboarding`, `onboarding-cro`, `docs-as-marketing` |
| B4 interop | `e2e-testing`, `debugging-toolkit` |
| B5 a11y | `accessibility-compliance-accessibility-audit`, `frontend-lighthouse`, `fixing-accessibility`, `playwright-skill` |
| B6 conversion | `onboarding-cro`, `page-cro`, `copywriting`, `frontend-seo`, `geo-fundamentals` |
| B7 security | `production-code-audit`, `security-and-hardening`, `find-bugs`, `pentest-checklist`, `mock-hunter` |
| B8 perf | `performance-engineer`, `performance-profiling` |
| C1 YC | `pitch-psychologist`, `product-marketing`, `objection-preemptor`, `clarity-gate` |
| C3 pricing | `pricing-strategy`, `price-psychology-strategist` |
| C4 GTM | `launch-strategy`, `growth-engine`, `cold-email`, `customer-research` |

**Gates before you declare a wave complete:** `pre-ship-gate`, `codebase-audit-pre-push`,
`brooks-review`, `brooks-test`.

---

## PART 7 — LANE PROMPT TEMPLATE

Every lane prompt must contain all nine blocks. The Wave A prompts were good — they had
identity, disjoint ownership, TDD, and "do not commit." Add the missing pieces: the ground-truth
warning, the baseline, the anti-weakening clause, and a required report format.

```
You are <LANE-ID>, a LongCat-2.0 subagent. <one-line role>.

[1] ENVIRONMENT — NON-NEGOTIABLE
Your shell cwd is C:\Users\Wasif. It is NOT the repo.
Every bash call MUST pass workdir = C:\Users\Wasif\Documents\Multiplayer-AI-isolated
Every read/write/edit MUST use an absolute path under that worktree.
NEVER touch C:\Users\Wasif\Documents\Multiplayer-AI (master, separate work).
Do NOT commit. Do NOT run git reset/checkout/clean.

[2] PROJECT
Weft — evidence-backed handoff/coordination layer for AI agents.
Python 3.11+, standard library only, SQLite state, zero runtime dependencies,
no CDN, no global installs. That constraint is a product promise, not a preference.

[3] GROUND TRUTH WARNING
AGENT_HANDOVER.md is authoritative EXCEPT §2.1, §2.2, §5 and §9, which are stale:
 - §2.1/§2.2 describe the DELETED "DOUBLE ENTRY" design. The live design is
   FIELD NOTES (Fraunces serif + Big Shoulders Display). Do NOT "fix" serif usage.
 - §5 describes Codex tools that do not exist here.
 - §9 names the wrong worktree.
§4.2 (traps) and §7 (security boundaries) ARE current — obey them.

[4] YOUR FILES — you own these exclusively, touch nothing else
<explicit list>
If your task requires editing a file you do not own, STOP and report it.
Integration/wiring is the orchestrator's job, not yours.

[5] READ FIRST
<3-5 specific files with a reason for each>

[6] TASK
<bounded, verifiable objective>

[7] METHOD — TDD, RED FIRST
Write the failing test first. Run it, confirm it FAILS, and paste that output.
Then implement. Then run again and paste the passing output.
A test written after the implementation does not count — say so if you did that.
NEVER weaken, skip, or delete an existing assertion to make a suite pass.
If a test encodes a genuinely dead requirement, say so explicitly and propose
the replacement invariant. Do not silently relax it.
No mocks for the SQLite layer — this project tests against real storage.

[8] VERIFY — paste real output for each
  python -B -m unittest tests.<your_module> -v
  python -B -m unittest discover -s tests 2>&1 | Select-Object -Last 5
  python -B scripts/weft-smoke.py     (if you touched core/server)
Baseline to compare against: docs/BASELINE_2026-08-05.md

[9] REPORT — exactly this shape, no prose padding
  FILES CHANGED: <path — one line each>
  TESTS ADDED: <n> — names
  TEST RESULT: <ran N, failures F, errors E>  [paste last 5 lines]
  BASELINE DELTA: <what moved vs baseline, and why>
  SMOKE: <evidence_passed value, or N/A>
  INVARIANTS TOUCHED: <from handover §4.2/§7, or none>
  NOT DONE: <anything you could not finish — this field is mandatory,
             write "nothing" only if genuinely nothing>
  RISKS: <what the orchestrator must check before merging>
```

---

## PART 8 — DEFINITION OF DONE

A wave is complete only when **all** of these hold, each backed by pasted output:

1. `python -B -m unittest discover -s tests` — 0 failures, 0 errors.
2. `python -B scripts/weft-smoke.py` — `evidence_passed: true`.
3. `node --check site/app.js` — clean.
4. `node scripts/capture-site-qa.cjs` — `consoleErrors: []`, all harness booleans true.
5. `python scripts/weft_performance_gate.py` — no regression against the locked baseline.
6. Every new module is imported by `core.py` or `server.py` and covered by one integration test.
7. Every number stated in `site/` and `docs/` was measured this session.
8. `AGENT_HANDOVER.md` updated: new modules in §3, new invariants in §7, new traps in §4.2,
   §8 gap list re-ordered.
9. One commit per lane, scoped paths, no `git add -A`.
10. Your summary distinguishes what you **verified** from what a lane **claimed**.

---

## PART 9 — SOURCES

**Repo — current and authoritative**
`AGENT_HANDOVER.md` §3, §4.2, §6, §7, §8, §10 · `docs/PROTOCOL.md` ·
`docs/PRODUCTION_PROTOCOL.md` · `docs/SECURITY_GATES.md` · `docs/PERFORMANCE.md` ·
`docs/AI_READABILITY.md` · `docs/GO_LIVE.md` · `docs/YC_READINESS.md` · `docs/PAIRING_UX.md` ·
`docs/IDENTITY_OIDC.md` · `docs/WEBSITE_COMPLETION_AUDIT_2026-07-31.md` · `design-qa.md` (except
line 5) · `.omx/goals/` ledgers

**Repo — stale, fix before trusting**
`AGENT_HANDOVER.md` §2.1, §2.2, §5, §9 · `design-qa.md:5` · every "65 tests" occurrence

**Wave A output — review before building on**
`docs/NEXUS_ARCHITECTURE.md` · `docs/PROTOCOL_V2.md` · `docs/adr/0001`, `docs/adr/0002`

**External**
YC application form and deadlines — verify live at ycombinator.com/apply; the July 27 date in
`YC_APPLICATION.md` was checked 2026-07-28 and is stale ·
MCP specification (modelcontextprotocol.io) for the compatibility boundary ·
WCAG 2.2 AA for B5 · MCP host docs for the B4 matrix (Claude Desktop, Claude Code, Cursor, Zed,
Continue)

---

## PART 10 — PRIORITY ORDER

If you do nothing else, do these five, in order:

1. **S1** — restore the two missing site files. The site bundle is broken right now.
2. **S2** — fix the handover's design section. Every site lane is currently primed to
   destroy the live design.
3. **S4** — the credential-rotation defect. A rotated token still authenticates. That is a
   security bug in a product whose entire pitch is a trust boundary.
4. **B4** — install it in two real MCP hosts. The central claim of the product has never
   been tested outside this machine.
5. **B2** — instrument time-to-first-verified-handoff. Without it the YC application cannot
   be truthfully completed, and no amount of new code changes that.

Items 1–3 are defects. Items 4–5 are the difference between a strong prototype and a company.
The engineering is already the strongest part of this project; it is not where the remaining
risk lives.
