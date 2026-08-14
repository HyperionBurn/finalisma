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

## Results

| agents | deliveries expected | losses | read failures | leaks | order violations | wake p50 | rate retries |
|--------|---------------------|--------|---------------|-------|-------------------|----------|--------------|
| 10     | 200                 | 0      | 0             | 0     | 0                 | 159.0 ms | 0            |
| 50     | 2500                | 0      | 0             | 0     | 0                 | 156.3 ms | 0            |

Selftest (deliberate routing break re-injecting the staleloss shape): harness
reports failure — the guard goes red.

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
