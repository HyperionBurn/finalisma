# Single-node performance envelope

Finalisma has a locked, standard-library-only performance gate for the local
SQLite coordinator. It measures product work rather than isolated helper
functions:

- authenticated routing across 32 agents with active load;
- paired session send, poll, and acknowledgement relay;
- authenticated task creation, claim, update, message, evidence verification,
  and completion.

## Current verified result

On the Windows development host used for the 2026-07-30 performance pass, the
seven-trial weighted median moved from **1,265.771 ms** to **59.314 ms**, a
**95.31% improvement**. All three scenario digests remained identical, every
scenario p95 improved by more than 91%, all 65 tests passed, the protocol smoke
flow passed, and evaluator output contained no raw actor credential.

This is a same-machine baseline comparison, not a universal latency promise.
Filesystem, antivirus, CPU, Python, and SQLite differences can materially
change absolute timings.

## Run the locked gate

```powershell
python -B .\scripts\finalisma_performance_gate.py `
  --baseline .omx\goals\performance\single-node-coordinator-envelope\baseline.json `
  --runs 7
```

The command fails unless the weighted median is at least 20% faster than the
locked baseline, no scenario p95 regresses by more than 5%, semantic digests
match, the full test suite and smoke flow pass, and output remains credential
safe.

Capture a baseline only before optimization, under a new reviewed performance
goal. Never overwrite a baseline to make a regression pass:

```powershell
python -B .\scripts\finalisma_performance_gate.py `
  --baseline .omx\goals\performance\<new-goal>\baseline.json `
  --runs 7 `
  --capture-baseline
```

## What changed

The largest Windows cost was opening and closing a SQLite connection for every
store operation. `FinalismaStore` now keeps bounded, thread-safe idle pools for
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

Long-lived callers should use `with FinalismaStore(...) as store:` or call
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
