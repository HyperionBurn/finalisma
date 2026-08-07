# Correction to the dogfood-fix brief — the tool rename MUST include site/web copy

My brief said "do not touch `video/`, `site/`, or `web/`". That was wrong for finding #1.

Renaming the MCP tools (dropping the redundant `finalisma_` prefix) is only complete if every
place that DISPLAYS a tool name is updated too. Those references exist in:

- `site/docs/quickstart.html`
- `site/index.html`
- `web/src/components/ConnectTiers.astro`
- `web/src/components/LiveDemo.astro`

**You MAY and SHOULD update those files — but ONLY the tool-name strings.** Do not restyle,
restructure, rewrite copy, or change anything else in them. A visitor copying a tool name off the
marketing site and getting "tool not found" is exactly the first-hour failure this whole exercise
is meant to eliminate.

`video/` stays untouched — the film shows no tool names.

**After editing anything under `web/src/`, the site must be rebuilt** so `site/` reflects it, and
`node web/scripts/verify-preservation.cjs` must pass with ZERO deletions. The build guard uses
explicit `--snapshot`/`--restore` now; if you see `.legacy-staging/` debris from an interrupted
run, the guard will warn and clear it — do not delete `site/` contents by hand.

`tests/test_site.py` asserts on site copy. If a tool name appears in an assertion, update it and
say so in your report.
