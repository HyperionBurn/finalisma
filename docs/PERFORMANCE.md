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

## Boundary

This gate proves the dependency-free single-node coordinator. It does not prove
hosted multi-tenant scale, cross-region latency, or multi-instance database
semantics. Those still require shared transactional storage, OAuth/OIDC, tenant
isolation, distributed rate limits, an outbox, and dedicated load/failure tests.
