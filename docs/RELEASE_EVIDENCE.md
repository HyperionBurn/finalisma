# Release evidence — 2026-08-17

This is the current local evidence ledger for the integration hardening work.
It is not a hosted-release approval, deployment proof, or third-party client
compatibility claim.

## Latest regression evidence

Measured by hosted CI on PR #28's final source/test stack (branch
`codex/weft-dashboard-contrast-2026-08-17`):

- Command: `python -B -m unittest discover -s tests -p "test_*.py"`
- Result: **1139 tests discovered; 1138 passed; 1 skipped**
- Duration: 351.238 seconds on the CI runner
- Focused bridge/identity/auth/web suite: 86 tests passed locally
- Homepage build: Astro build and legacy-preservation checks passed
- `git diff --check`: passed

At capture time, stacked PRs #24–#28 were open with both CI jobs successful;
none had been merged or deployed.

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
