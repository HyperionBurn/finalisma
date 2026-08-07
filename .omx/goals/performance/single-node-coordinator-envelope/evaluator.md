# Performance Evaluator: single-node-coordinator-envelope

## Objective
Measure and improve Weft's realistic single-node coordination envelope, reducing weighted median coordinator wall time by at least 20 percent while preserving protocol semantics, security boundaries, and product behavior.

## Evaluator Command
```sh
python -B scripts/weft_performance_gate.py --baseline .omx/goals/performance/single-node-coordinator-envelope/baseline.json --runs 7
```

## Pass/Fail Contract
PASS only when seven-trial weighted median wall time is at least 20 percent faster than the locked baseline, no scenario p95 regresses by more than 5 percent, benchmark result digests exactly match baseline semantics, all 53 unit and site tests pass, the protocol smoke flow passes, and no raw actor credential appears in evaluator output. Otherwise FAIL.

This evaluator must exist and produce concrete pass/fail evidence before the performance goal can be completed.
