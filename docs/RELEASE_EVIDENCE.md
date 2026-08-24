# Release evidence — 2026-08-24

This is the current local evidence ledger for the integration hardening work.
It is not a hosted-release approval, deployment proof, or third-party client
compatibility claim.

## Latest regression evidence

Measured locally on the current source/test stack (merged base `main` at
`ab809cf`; hosted-MCP schema clarity changes in the working tree):

- Command: `python -B -m unittest discover -s tests`
- Result: **1312 tests discovered; 1311 passed; 1 skipped**
- Duration: 619.429 seconds on the local Windows runner
- Focused API-readiness routing/identity suite: 69 tests passed locally
  (`tests.test_probe_live_release`, `tests.test_probe_live_release_contract`,
  `tests.test_deploy_gate`, and `tests.test_deploy_ops`)
- Focused hosted SDK connect suite: 33 tests passed locally
  (`tests.test_interop_sdk` and `tests.test_sdk`)
- Focused storage-readiness/deploy-gate suite: 47 tests passed locally
  (`tests.test_probe_live_release`, `tests.test_probe_live_release_contract`,
  and `tests.test_deploy_gate`)
- Focused web/deploy readiness suite: 69 tests passed locally
- Focused hosted-MCP probe/preflight/workflow suite: 37 tests passed locally
- Focused rollback/deploy/preflight suite: 61 tests passed locally
- Focused hosted-MCP/count suite: 48 tests passed locally
  (`tests.test_hosted_mcp` and `tests.test_site.TestCountSyncTests`)
- Focused hosted SDK/onboarding suite: 97 tests passed locally
  (`tests.test_interop_sdk`, `tests.test_onboarding_consistency`, and
  `tests.test_cloud_service`)
- Focused creator-key onboarding suite: 31 tests passed locally
  (`tests.test_hosted_mcp`, `tests.test_onboarding_consistency`, and
  `tests.test_site.TestCountSyncTests`)
- Release-boundary checks passed locally, including Vercel
  materialization, live-probe contracts, and Compose safety contracts
- Self-hosted cursor/filter subset: 10 tests passed locally, including the
  64-item message-kind boundary
- Live-probe diagnostics contract: PASS returns no diagnostics, while page and
  manifest drift return redacted check-level diagnostics
- Diagnostic redaction contract: redirect userinfo, query strings, fragments,
  and transport-error details are excluded from emitted endpoint facts
- Compose parser: not run locally because Docker is not installed; CI must run
  `docker compose -f compose.yaml config --quiet`
- Site build: `npm run build` passed with no app origin. A configured-origin
  probe with `https://app.example.test` rendered that origin in the SDK `/mcp`
  example, then the no-origin build was restored. The rendered Playwright
  harness refreshed `artifacts/design-qa/qa-results.json` with exit 0
- Rendered UX fallback: the browser harness proved the denied-clipboard path
  leaves a labelled brief visible, focused, and prepared; no console errors,
  failed requests, or HTTP error responses occurred, and axe reported zero
  violations across eight scanned pages
- `git diff --check`: passed

## Hosted customer journey evidence

The backend release at commit `f941d89` was deployed through the strict
archive gate. The gate ran **1312 tests with 0 failures**, then passed the
WAL-safe backup and restore drill, service restart proof, public readiness,
and unauthenticated MCP routing checks.

- `scripts/probe_live_release.py` against the public API and Vercel site:
  **PASS**, with no diagnostics and all release-alignment checks true.
- `scripts/healthcheck.py --base-url
  https://weft.switzerlandnorth.cloudapp.azure.com/v1
  --edge-url https://weft.switzerlandnorth.cloudapp.azure.com
  --require-https-edge`: **healthcheck ok**. The cloud base and public edge
  are deliberately probed separately because `/readyz` at the public root
  belongs to the web process, while `/v1/readyz` belongs to `weft-cloud`.
- `scripts/probe_live_customer_journey.cjs` passed at **1440x900** and
  **390x844**. Each fresh browser context completed the real site CTA →
  signup → login → dashboard → room creation → connect page → OpenCode
  connector-config generation → wrong-confirmation refusal → exact-confirmation
  organization deletion flow. Each run reported 0 console errors, 0 failed
  requests, 0 unexpected responses, and completed disposable-state cleanup.
- `scripts/probe_live_opencode.cjs` launched the installed OpenCode **1.18.22**
  process with the generated OpenCode 1.x config and the exact requested model
  `opencode-go/deepseek-v4-pro`. The provider rejected the run before any MCP
  tool call with `Insufficient balance`. The probe deleted the disposable
  organization afterward. This is an external provider-billing blocker, not a
  Weft protocol result.

This is hosted Weft browser evidence. It does not claim SMTP mailbox delivery,
Claude/Cursor/Codex host execution, or a successful OpenCode model call while
the provider balance is insufficient.

Hosted CI, review, merge, and deployment status must be read from the linked
pull requests rather than inferred from this local ledger. The focused result
is local evidence.

## Explicitly unverified here

- Docker image build and Compose lifecycle
- VM systemd installation, SMTP credentials, mailbox delivery, and worker health
- Public-edge invite opening and acceptance
- Live Claude, Cursor, or Codex host-product launch; OpenCode was launched but
  its provider stopped before MCP calls because of insufficient balance
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
