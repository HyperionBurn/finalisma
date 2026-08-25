# Scale proof — measured results

Date: 2026-08-14. Harness: `scripts/scale_proof.py` (committed, re-runnable,
stdlib only). Target: local cloud instance (`WeftCloudService` over real
loopback HTTP — the same surface an MCP host uses), 1 account + N-1 agent keys
(the production connector topology), Pro plan.

The acceptance bar (agreed in the build room):
1. delivery loss = 0 (receipts are exhaustive ground truth),
2. sampled read-back: recipients can read what was addressed to them,
3. unicast leak = 0 (non-addressees get redacted/not_the_addressee, payload
   replaced wholesale),
4. per-recipient ordering strictly increasing (subsequence of global),
5. wake latency p50 measured from real room_wait returns,
6. the harness must go RED against a deliberately broken build.

## Results — 2026-08-14 (historical; superseded by the 2026-08-15 re-measure below)

| agents | deliveries expected | losses | read failures | leaks | order violations | wake p50 | rate retries |
|--------|---------------------|--------|---------------|-------|-------------------|----------|--------------|
| 10     | 200                 | 0      | 0             | 0     | 0                 | 159.0 ms | 0            |
| 50     | 2500                | 0      | 0             | 0     | 0                 | 156.3 ms | 0            |

Selftest (deliberate routing break re-injecting the staleloss shape): harness
reports failure — the guard goes red.

## Results — 2026-08-15 re-measure (independent verification)

Same harness (`scripts/scale_proof.py`), same target (local cloud instance,
1 account + N-1 agent keys, Pro plan). The harness now also emits wake p95;
the run's full JSON report is the ground truth. Two full 50-agent soaks were
run back-to-back on 2026-08-15 and both passed; the table records the run
with p95 captured, and the first soak (pre-p95 instrumentation) passed at
wake p50 126.7 ms with 711 wake samples.

| agents | deliveries expected | losses | read failures | leaks | order violations | wake p50 | wake p95 | room rate retries | ip rate retries |
|--------|---------------------|--------|---------------|-------|-------------------|----------|----------|-------------------|-----------------|
| 10     | 200                 | 0      | 0             | 0     | 0                 | 125.6 ms | 241.9 ms | 0                 | 0               |
| 50     | 5000                | 0      | 0             | 0     | 0                 | 133.2 ms | 242.3 ms | 0                 | 11              |

Notes on the 2026-08-15 numbers:

- `deliveries_expected` = cycles × (broadcasts × (agents−1) + unicasts). The
  50-agent run completed 2 full cycles in the 10-minute soak window
  (2 × 2500 = 5000), matching the per-cycle accounting of the 2026-08-14
  2500 figure.
- The 11 `ip_rate_retries` on the 50-agent run are the per-IP request limiter
  returning 429 during verification polling; the harness retries honestly
  after `retry_after` and reports the count. Zero `rate_limited` retries on
  room sends — the 1.25 s global send pace stayed under the 60/min room
  budget. The 10-agent run and the first 50-agent soak both measured 0.
- Identity topology held on every run: roster length asserted == agents
  (50 distinct key identities, 1 account + 49 agent keys), keys-identity
  mode.
- Selftest re-run on 2026-08-15 against the final harness state: the
  injected routing break was detected (losses 38 reported, `pass: false`,
  exit 0 with `SELFTEST PASS`). The guard goes red.

Provenance: all 2026-08-15 numbers above were produced by running, in this
order, on 2026-08-15 on the harness's local target:

```powershell
python -B scripts/scale_proof.py --selftest
python -B scripts/scale_proof.py --agents 50 --minutes 10 --plan pro
python -B scripts/scale_proof.py --agents 10 --minutes 1 --plan pro
python -B scripts/scale_proof.py --selftest
```

Re-run to reproduce; do not copy into other documents without re-measuring.

## Plan boundaries measured along the way (each is a product fact)

1. Free plan caps rooms at 10 members (`quota_exceeded`, `max_members_per_room`
   = 10). The 50-agent claim requires the Pro plan, whose cap is 50.
2. Free AND Pro both cap room-wide message rate at 60/min (`quotas.py`), so a
   50-agent room sustains at most ~1.2 messages/agent/min through the room
   budget. The harness paces sends globally at 1.25s and reports any
   rate_limited retry; zero were needed at the paced rate.
3. One IP hosting N agents collides with the per-IP request limiter: rapid
   50-account signups return 429 (the 20/IP/900s auth limiter), and
   exhaustive O(N^2) verification polling returns 429 on /mcp. Two
   consequences: (a) agent keys, not per-agent signups, are the viable
   multi-agent topology; (b) verification traffic must be stratified, not
   exhaustive. Both are encoded in the harness (`--identity keys` default,
   bounded verification samples).

Provenance: all numbers above were produced by running
`python -B scripts/scale_proof.py --agents N --minutes M --plan pro` on
2026-08-14 on the harness's local target. Re-run to reproduce; do not copy
into other documents without re-measuring.
