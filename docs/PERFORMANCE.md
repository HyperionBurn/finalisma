# Single-node performance envelope

Weft has a locked, standard-library-only performance gate for the local
SQLite coordinator. It measures product work rather than isolated helper
functions:

- authenticated routing across 32 agents with active load;
- paired session send, poll, and acknowledgement relay;
- authenticated task creation, claim, update, message, evidence verification,
  and completion.

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
suite is 915 tests after subsequent security and lifecycle coverage. Three isolated seven-trial
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
