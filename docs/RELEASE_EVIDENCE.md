# Release evidence — 2026-08-20

This is the current local evidence ledger for the integration hardening work.
It is not a hosted-release approval, deployment proof, or third-party client
compatibility claim.

## Latest regression evidence

Measured locally on the current source/test stack (merged base `main` at
`bf3a82e`; a feature branch may be active while a PR is in flight):

- Command: `python -B -m unittest discover -s tests -p "test_*.py"`
- Result: **1205 tests discovered; 1204 passed; 1 skipped**
- Duration: 555.020 seconds on the local Windows runner
- Focused hosted-MCP/count suite: 36 tests passed locally
  (`tests.test_hosted_mcp` and `tests.test_site.TestCountSyncTests`)
- Release-boundary subset: 18 tests passed locally, including Vercel
  materialization, live-probe contracts, and Compose safety contracts
- Self-hosted cursor/filter subset: 10 tests passed locally, including the
  64-item message-kind boundary
- Compose parser: not run locally because Docker is not installed; CI must run
  `docker compose -f compose.yaml config --quiet`
- Site build: `npm run build` passed with no app origin, and the rendered
  Playwright harness refreshed `artifacts/design-qa/qa-results.json` with exit 0
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
