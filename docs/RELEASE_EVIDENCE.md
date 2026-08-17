# Release evidence — 2026-08-17

This is the current local evidence ledger for the integration hardening work.
It is not a hosted-release approval, deployment proof, or third-party client
compatibility claim.

## Latest regression evidence

Measured locally on PR #35's current source/test stack (branch
`codex/weft-live-outbox-lease-heartbeat-2026-08-17`):

- Command: `python -B -m unittest discover -s tests -p "test_*.py"`
- Result: **1141 tests discovered; 1140 passed; 1 skipped**
- Duration: 487.830 seconds on the local Windows runner
- Focused SDK/interop/count-guard suite: 29 tests passed locally
- Site build: not rerun in this SDK-only local command; prior site evidence
  remains on the earlier stacked PRs
- `git diff --check`: passed

At capture time, stacked PRs #24–#35 were open; PR #35's hosted CI and merge
status must be read from GitHub rather than inferred from this ledger.

The suite result is hosted CI evidence for this checkout. Hosted CI, review,
merge, and deployment status must be read from the linked pull requests rather
than inferred from this document. The focused result is local evidence.

## Explicitly unverified here

- Docker image build and Compose lifecycle
- VM systemd installation, SMTP credentials, mailbox delivery, and worker health
- Public-edge invite opening and acceptance
- Live Claude, Cursor, Codex, or OpenCode host-product launch
- Production uptime, external latency, customer adoption, or paid retention

The repository intentionally keeps these boundaries visible. A local protocol
or in-process test is not a substitute for the corresponding hosted or
third-party proof.

## Refresh procedure

Run the full command above, record the exact discovered/passed/skipped result,
update this ledger and any current-facing summary only from that output, then
run `tests.test_site.TestCountSyncTests` and inspect hosted CI. Historical
benchmark counts remain in their dated provenance documents and must not be
rewritten as current evidence.
