# Release evidence — 2026-08-17

This is the current local evidence ledger for the integration hardening work.
It is not a hosted-release approval, deployment proof, or third-party client
compatibility claim.

## Latest local regression

Measured against commit `5ef5011` on branch
`codex/weft-web-room-errors-2026-08-17`, before the documentation-only changes
in this release-truth PR:

- Command: `python -B -m unittest discover -s tests -p "test_*.py"`
- Result: **1136 tests discovered; 1135 passed; 1 skipped**
- Duration: 436.155 seconds
- Focused browser room suite: 31 tests passed
- Compose YAML parse: passed
- `git diff --check`: passed

The suite result is local evidence from this checkout. Hosted CI, review,
merge, and deployment status must be read from the linked pull requests rather
than inferred from this document.

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
