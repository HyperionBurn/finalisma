# Single-node performance envelope

Weft has a locked, standard-library-only performance gate for the local
SQLite coordinator. It measures product work rather than isolated helper
functions:

- authenticated routing across 32 agents with active load;
- paired session send, poll, and acknowledgement relay;
- authenticated task creation, claim, update, message, evidence verification,
  and completion.

## 2026-08-28 MPAI-102 browser measurement — deferred landing hero video

This dated browser measurement is the before/after evidence for commit `85e953f`
on the live landing page. It is separate from the single-node coordinator gate
below. The before run was captured at `2026-08-28T08:36:15.953Z` against the
then-live page; the after run was captured at `2026-08-28T16:23:27.492Z` after
deploy `5b463c1`.

| landing capture | transfer | HTTP requests | FCP | LCP | main-thread TaskDuration |
| --- | ---: | ---: | ---: | ---: | ---: |
| before, eager video | 1,546,280 B | 13 | 712 ms | 784 ms | 329.0 ms |
| after, no interaction | 442,231 B | 12 | 1,540 ms | 1,936 ms | 340.8 ms |

The deferred path removes `1,104,049 B` (`71.4%`) and one initial request. The
before capture fetched `/assets/hero.mp4` at `1,106,210 B`; the after capture
did not request it before the observation window. The after capture's FCP and
LCP were `828 ms` and `1,152 ms` slower than the before capture, respectively,
so this run proves a substantial transfer reduction but does not prove a paint
time improvement or attribute that slower paint to the video change. The same
after window also showed slower paints across authenticated app routes, making
the live-origin timing shift a measured confounder.

Two live behavior checks followed the after sweep. At
`2026-08-28T16:27:08.810Z`, a fresh reduced-motion (`prefers-reduced-motion:
reduce`) context made `12` requests totaling `442,231 B`, made zero
`hero.mp4` requests, and left the video with `preload="none"`, no `src` or
`currentSrc`, `paused=true`, and `readyState=0`; there were no console errors.
At `2026-08-28T16:28:26.004Z`, a normal fresh context made zero hero requests
before a synthetic `pointerdown`, then exactly one request after it; the video
resolved `currentSrc` to `hero.mp4`, was not paused, and reached `readyState=4`.

Method for all captures: Node Playwright `1.62.1` driving system Chrome at
`1440x900`; a fresh browser context per route/sample; service workers blocked;
cache disabled; unique query-string cache buster; three runs per route with
medians; no network throttling. Transfer used CDP Network
`loadingFinished.encodedDataLength` (plus received bytes for still-open
requests), main-thread cost used the CDP Performance `TaskDuration` delta, and
FCP/latest LCP came from an early buffered `PerformanceObserver` read after a
fixed `5,000 ms` post-DOM observation window. The no-interaction after run is
the first-visit measurement; the pointerdown run is a separate preservation
check. All figures in this section come from those captures.

## 2026-08-29 MPAI-102 browser measurement — poster preload on deploy `d4fb469`

This is the follow-up after deploy `d4fb4697be9d734fbb79a79a4ffbfe472dc797cc`
(`d4fb469`) to `https://finalisma.vercel.app` (Vercel deployment
`dpl_DEFVnauN5DTt7oV9GBaT9c8xDc6d`, production). The run was captured at
`2026-08-29T15:04:55.264Z` with the private `mpai102_measure_after.cjs`
harness. The binary card criterion is the first line:

**`/assets/hero.mp4` was requested zero times before user interaction (0 B) in
all three landing samples.**

The primary landing comparison is absolute, against the like-for-like pre-fix
landing capture above. It is not a landing-minus-control before/after delta:
the pre-fix landing capture did not include a same-window control route.

| landing capture | transfer | HTTP requests | FCP | LCP | main-thread TaskDuration |
| --- | ---: | ---: | ---: | ---: | ---: |
| pre-fix baseline, eager video (2026-08-28) | 1,546,280 B | 13 | 712 ms | 784 ms | 329.0 ms |
| post-fix, no interaction (2026-08-29) | 214,741 B | 12 | 372 ms | 372 ms | 315.5 ms |

Relative to that pre-fix capture, the post-fix landing transferred `1,331,539 B`
less (`86.1%`) and made one fewer request; FCP was `340 ms` lower, LCP was
`412 ms` lower, and TaskDuration was `13.5 ms` lower in this window. The
landing samples fetched the poster once each (`22,184`, `22,209`, and
`22,210 B`; median `22,209 B`), had no video preload hint, had the poster
image preload in the DOM, and had no console errors. A separate cache-busted
document-only fetch measured `25,282 B`; that is not contradictory to the
`214,741 B` total browser transfer, which includes the document's CSS, JS,
fonts, poster, and other first-view resources. The previous document-only
check was `25,439 B` while the previous total-transfer baseline was
`1,546,280 B`.

The browser identified the LCP element as `VIDEO.plate-video` in all three
landing samples, with `poster="/assets/hero-poster.webp"`, no `src`, and no
`currentSrc`. This directly verifies that the poster-backed video element, not
an inferred resource, was the LCP candidate after the preload fix.

The secondary unchanged control route was `/app/connect/` on the service origin
(`https://weft.switzerlandnorth.cloudapp.azure.com`), because Vercel's
`/app/*` redirect deliberately sends that route there. Its same-window median
was `282,593 B / 13 requests / FCP 536 ms / LCP 564 ms / TaskDuration 86.9 ms`
(three interleaved samples; range `282,593–283,021 B`). It is environment
context, not the headline and not a pre-fix delta. The landing-versus-control
same-window differences were `-67,852 B`, `-1` request, `-164 ms` FCP,
`-192 ms` LCP, and `+228.6 ms` TaskDuration. One control sample logged a 404
console error; landing logged none. The control route reached the old backend
and used a probe session only to render the authenticated path; this browser
measurement makes no claim that backend changes were deployed or verified.

Method for this follow-up: one Chrome instance, three interleaved route pairs
in order landing/control, control/landing, landing/control; fresh context for
each sample; viewport `1440x900`; cache disabled; service workers blocked;
unique `mpai102after` query strings; no pointer, keyboard, scroll, touch, or
other user input; and a fixed `5,000 ms` post-DOM observation window. Transfer
used CDP encoded bytes, hero-fetch status counted any `/assets/hero.mp4`
request during navigation plus that no-input window, preload presence came
from the DOM, and FCP/LCP/TaskDuration used the same CDP/Performance methods
as the earlier section. The earlier post-deferral run remains historical
evidence: it measured lower transfer but slower FCP/LCP, and authenticated
routes also slowed then, so its caveat about causal paint attribution remains
visible. This new run records the observed recovery on the deployed origin; it
does not erase that earlier uncertainty.

## Current verified result (latest local run)

On the Windows development host, the reference file records a seven-trial
weighted median of **72.221 ms** (weighted p95 81.899 ms). The latest complete
gate run on 2026-08-13 measured **137.404 ms** (weighted p95 **155.381 ms**)
and therefore **failed**: weighted median improvement was -90.25%, and scenario
p95 changed by +82.59% (`routing_fanout`), +98.81% (`session_relay`), and
+81.84% (`authenticated_core`). The quality sub-gates did pass for that
historical **894-test snapshot**, along with protocol smoke and credential-output checks. A long-running OpenCode process
was consuming substantial CPU and memory during this capture. That is a
diagnostic confounder, not proof that host load caused the entire regression;
the gate remains red until a controlled idle-host rerun explains or clears it.

2026-08-14 note (supersedes for the current tree): three single-trial gate
runs on 2026-08-14 measured weighted medians of ~144.8 ms, ~68.8 ms, and
~145.0 ms against the same 72.221 ms reference (-100.5%, +4.5%, -100.6%
"improvement" respectively) — run-to-run variance of this size on the same
tree is host load, not code: the regressing scenarios (`session_relay`,
`routing_fanout`) were untouched by the changes under test, and multiple
OpenCode/agent sessions were active on the host during every capture. The one
code change that DOES touch the coordinator hot path — the presence touch —
was optimized in this tree to an in-memory throttle (zero DB work inside the
window); the immediately-following run moved from -50.6% to +4.5% median,
consistent with that fix. The gate remains red pending a controlled idle-host
rerun, per the standing rule: never rebaseline to hide a regression, never
report a performance figure that was not measured under documented
conditions.

2026-08-15 note (supersedes for the current tree): two complete seven-trial
gate runs on 2026-08-15, back to back on the same tree with zero code change,
measured weighted medians of **71.531 ms** and **76.553 ms** against the
72.221 ms reference (+0.96% and -6.00% "improvement"). Run 1 failed only the
scenario-p95 guards: `routing_fanout` p95 +66.10%, `session_relay` p95 +75.60%
(medians +0.39% and +5.64%). Run 2 failed the weighted-median guard and
`session_relay` p95 +31.46% (median +19.65%) while `routing_fanout` swung to
-4.74% median / -12.52% p95. A 78-point p95 swing on an untouched scenario
between consecutive runs is the measurement noise floor on this host, not code.
Both runs passed all quality sub-gates (960 tests, protocol smoke, no raw
credential in output).

Profiling evidence, same host, same day (scratch harness in the temp dir, not
the repo): per-operation medians in the session relay loop are send 0.084 ms,
poll 0.050 ms, ack 0.033 ms (160 of each per trial) — the loop is I/O bound with
no N+1 queries or re-reads (statement economy is pinned by
`tests/test_performance_hotpaths.py`). Two stall classes remain per trial:
(a) a deterministic ~5-6 ms WAL-checkpoint stall that a scratch experiment
(`PRAGMA wal_autocheckpoint=10000`) removes, shaving ~5 ms/trial (~10% of the
scenario), and (b) a ~10-13 ms stall that hits even read-only operations
(`session_poll` max 8.9-12.1 ms in every run; one 12.75 ms stall in 320
read-only `route_task` calls whose p99 is otherwise 0.241 ms). Class (b) is
host noise — it cannot come from coordinator code and is consistent with the
long-running agent sessions active on this host. The gated code also cannot
have regressed by construction: the last `core.py` change predates the
2026-08-13 10:37 baseline capture, and every post-baseline commit touches only
`room.py`, which the gate's locked scenarios never execute. Per the standing
rule the baseline was **not** touched and nothing was rebaselined. Candidate
micro-optimizations found but deliberately not applied in this lane (their
combined ceiling is ~10-15% on one scenario and cannot close a ±20-30% noise
gap): WAL-checkpoint deferral (~5 ms/trial on `session_relay`) and per-agent
capability-parse caching in `_route` (~10% of `routing_fanout`). The gate
remains red pending a controlled idle-host rerun; on this host it cannot
distinguish a true regression from load noise.

The preceding complete run measured **90.158 ms** (weighted p95 104.705 ms),
also failed the weighted-median and scenario-p95 guards, and passed the then-current
894-test quality snapshot,
protocol smoke, and credential-output checks. It remains historical evidence,
not the current result.

The reference metadata now uses the explicit `benchmark-critical-ast-v1`
digest scope. Its prior whole-file hash (`bed1…`) is retained as
`legacy_harness_sha256` because it was captured from a transient source state;
the scoped digest matches the committed benchmark logic and is unaffected by
quality-runner timeout/comment changes. This repairs provenance without
changing any timing sample; the complete rerun above verified the repaired
contract.

An earlier complete run measured 71.349 ms / 79.400 ms p95, but failed a
single session-relay p95 guard (+20.15%) and initially exposed a stale published
test count. The historical benchmark-run count is 894; the current repository
suite was 915 tests at that point after subsequent security and lifecycle coverage (960 as of 2026-08-15). Three isolated seven-trial
captures on the same host produced weighted medians of 73.981 ms, 87.975 ms,
and 101.820 ms while the machine was under its normal background workload.
That spread is evidence of measurement variance, not a stable optimization or
regression claim. Do not overwrite the reference file to make a run pass;
rerun on a controlled host before publishing a current performance number.

**Provenance — do not confuse the numbers:**

| Measurement | Weighted median | Harness | Status |
| --- | --- | --- | --- |
| Original pre-optimization reference (initial commit `b4f3026`) | 1,265.771 ms | 3-scenario harness | Historical — the 95.31% improvement was claimed against this |
| Re-captured baseline (2026-08-05) | 68.844 ms | Extended harness (roster/tenancy scenarios added) | Superseded by the 2026-08-10 re-baseline |
| Reference file (current artifact) | 72.221 ms | Benchmark-critical AST v1 | Timing guards currently reject the run |

The published "95.31% faster (1,265.771 ms → 59.314 ms)" figure was measured
against a 3-scenario harness that no longer exists — the harness was extended
on 2026-08-05 with `roster_routing` and `tenancy_assert_scope` scenarios. The
old and new numbers are **not directly comparable** as a single before/after
pair. The original baseline and its 95.31% claim are preserved in the baseline
file's `history` array and in `docs/BASELINE_2026-08-05.md`. Any statement
about current performance must use the re-baselined measurement above.

This is a same-machine baseline comparison, not a universal latency promise.
Filesystem, antivirus, CPU, Python, and SQLite differences can materially
change absolute timings. The 2026-08-13 capture ran while an OpenCode process
was active and is therefore useful as a red gate result but insufficient to
attribute the regression to product code or host load alone. Re-run on an idle,
documented host before treating the timing as a stable release characteristic.

## Run the locked gate

```powershell
python -B .\scripts\weft_performance_gate.py `
  --baseline .omx\goals\performance\single-node-coordinator-envelope\baseline.json `
  --runs 7
```

The command fails unless the weighted median is at least the baseline's
recorded improvement target faster than the locked baseline, no scenario p95
regresses by more than 5%, semantic digests match, the full test suite and
smoke flow pass, and output remains credential safe.

The improvement target lives in the baseline file
(`target_improvement_percent`). A fresh capture sets it to 20%; a force
re-capture of an existing baseline (a post-optimization re-baseline) sets it
to 0 and turns the gate into a regression guard, preserving the previous
baseline in the `history` array. That is the honest way to re-baseline after
the harness changes.

Capture a baseline only under a new reviewed performance goal. Never overwrite
a baseline to make a regression pass:

```powershell
python -B .\scripts\weft_performance_gate.py `
  --baseline .omx\goals\performance\<new-goal>\baseline.json `
  --runs 7 `
  --capture-baseline
```

## What changed

The largest Windows cost was opening and closing a SQLite connection for every
store operation. `WeftStore` now keeps bounded, thread-safe idle pools for
write and query-only connections. Each pool retains at most four connections;
additional concurrent connections are closed when returned. SQLite remains the
source of truth, separate processes still coordinate through WAL, and no token
or message payload is cached outside SQLite.

The hot paths also avoid redundant work:

- WAL mode is established once during initialization;
- actor credential and agent authorization use one joined lookup;
- routing computes all active-agent loads in one set query;
- session send/poll/ack reuse known rows and cursor state;
- task writes use `RETURNING` instead of immediately re-reading rows;
- inbox reads reuse acknowledgement state from their existing join.

Long-lived callers should use `with WeftStore(...) as store:` or call
`store.close()` during shutdown. The CLI, smoke flow, pruning utility, and test
harnesses exercise this lifecycle.

## Wave A hot-path scenarios (roster, tenancy)

Wave A added `roster.py` (N-way roster routing) and `tenancy.py` (org scope
enforcement) to the hot path. These modules are **not yet mounted into
core/server** (S5 wiring follows this gate), so the scenarios below run
through the module APIs directly (`RosterStore` / `tenancy` module
functions) against their own SQLite tables. This boundary is intentional:
the locked single-node composite above is untouched, and these measurements
establish the per-call cost that S5 must budget for.

Measured on the same Windows development host, seven-trial medians:

| scenario | median (ms) | p95 (ms) | digest |
| --- | --- | --- | --- |
| `roster_routing` (64-member roster, 200 × 4 route_targets calls) | 1110.39 | 1218.702 | `9a14b9f2…` |
| `tenancy_assert_scope` (300 × 3 calls, pass + fail paths) | 83.131 | 89.626 | `52ada121…` |

Per-call cost: `route_targets` ≈ **1.39 ms/call** (material — exceeds the
1 ms/op threshold); `assert_scope` ≈ **0.09 ms/call** (not material).

**Recommendation for S5 wiring:** `route_targets` opens its own transaction
and runs three queries per call (active members, group map, then expansion).
At 1.39 ms/call it is the single most expensive new path. S5 should (a)
batch roster fan-out so one `route_targets` call serves a whole envelope
rather than one per recipient, and (b) consider reusing the core store's
connection pool instead of opening a fresh connection per call. The
`assert_scope` cost is negligible and needs no mitigation.

## Boundary

This gate proves the dependency-free single-node coordinator. It does not prove
hosted multi-tenant scale, cross-region latency, or multi-instance database
semantics. Those still require shared transactional storage, OAuth/OIDC, tenant
isolation, distributed rate limits, an outbox, and dedicated load/failure tests.
The Wave A hot-path scenarios measure module-level APIs only; they do not
exercise the wired S5 request path, which will have additional overhead.
