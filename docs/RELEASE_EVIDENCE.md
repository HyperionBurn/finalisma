# Release evidence — 2026-08-17

This is the current local evidence ledger for the integration hardening work.
It is not a hosted-release approval, deployment proof, or third-party client
compatibility claim.

## Latest regression evidence

Measured locally on the current source/test stack (branch
`codex/weft-mcp-envelope-guard-2026-08-17`):

- Command: `python -B -m unittest discover -s tests -p "test_*.py"`
- Result: **1160 tests discovered; 1159 passed; 1 skipped**
- Duration: 455.276 seconds on the local Windows runner
- Focused hosted-MCP/count suite: 34 tests passed locally
  (`tests.test_hosted_mcp` and `tests.test_site.TestCountSyncTests`)
- Site build: not rerun in this SDK-only local command; prior site evidence
  remains on the earlier stacked PRs
- `git diff --check`: passed

Hosted CI, review, merge, and deployment status must be read from the linked
pull requests rather than inferred from this local ledger. The focused result
is local evidence.

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
