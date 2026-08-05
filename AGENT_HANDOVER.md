# Finalisma — agent handover

> Read this before changing anything. This is the shortest complete explanation of
> what this repository is, what has already been built, what is verified, and what
> must not be accidentally broken.

The current website release audit is
[`docs/WEBSITE_COMPLETION_AUDIT_2026-07-31.md`](docs/WEBSITE_COMPLETION_AUDIT_2026-07-31.md).
Read it for the exact local PASS evidence and the two external values still
required before public deployment.

## 1. The product in one paragraph

Finalisma is a dependency-free MCP coordination layer for making two separate AI
agents behave like a governed team. The product promise is:

> Finalisma is designed for two MCP-capable hosts with compatible stdio or
> Streamable HTTP integration. They can share a scoped task, messages, leases,
> ordered events, and evidence without sharing provider credentials or conversation
> history. Real host interoperability validation is still pending.

The first startup wedge is evidence-backed handoffs for AI-native engineering teams:
incident triage and pull-request review. One agent opens a narrowly scoped task;
the other previews/consents, claims it under a lease, works inside the declared
scope, returns evidence, and can only complete after every check passes.

Finalisma coordinates agents. It does not execute arbitrary shell commands from
payloads, silently substitute model providers, or mutate host settings.

## 2. Current state at handoff

The repository contains both a real local MCP server and a no-build launch site.
The launch site has since been rebuilt as **DOUBLE ENTRY** (§2.1). The scroll-story
demo survived that rebuild as the reconciliation spread in folio 03:

- The page scrolls naturally through a tall track (`.recon-track`).
- The pinned viewport (`.recon-sticky`) stays below the site header, at the offset
  declared once as `--header-h` (currently 64px).
- Scroll progress posts a deterministic ledger from 0 to 7 entries, draining red as
  it goes, and moves the account from `OUT OF BALANCE` to `BALANCED`.
- Post first entry / Post next / Reverse and replay controls remain available and
  work independently of scroll.
- `prefers-reduced-motion` removes the travel entirely and keeps a readable,
  normal-flow manual ledger.
- No GSAP package or CDN was added, and no Lenis. This is a zero-install native
  equivalent using `position: sticky`, passive scroll, `requestAnimationFrame`,
  CSS variables, and native scroll-driven animations.

This browser demo is deliberately labeled as a simulation. It is not proof that two
real external hosts are already connected.

### 2.1 The design — DOUBLE ENTRY

Two earlier design passes ("control-room broadsheet", cream + Instrument Serif) were
**deleted**, not extended. The reason is recorded here so nobody restores them: serif
type over warm paper is now the AI-industry house style — Claude, Perplexity, Runway
and Manus have all converged on it, and the press calls it "tasteslop". Those passes
were well-executed members of exactly that family. Adding more craft to them would
have made the resemblance stronger, not weaker.

**The page is a ledger.** One hard vertical rule runs the full height of the document
at 37%: left of it is *asserted*, right of it is *proved*. Ten sections became seven
folios (`00 OPENING`, `01 THE UNPOSTED ACCOUNT`, `02 THE ENTRIES`,
`03 RECONCILIATION`, `04 THE AUDIT`, `05 QUERIES`, `06 CLOSING THE BOOKS`). Both fake
product mockups — the hero ledger card and the demo console window — are gone; they
were the loudest generated-artifact tell on the page.

Load-bearing rules:

- **The rule is painted once**, as a fixed backdrop (`.ledger-ground`, `z-index: -1`)
  with `::before` for the credit paper and `::after` for the hairline. Drawing it
  per-section is how gaps appear at section boundaries. Do not re-introduce that.
- **Percentages must resolve against the same width.** `.ledger-ground`,
  `.band`, `.folio`, `.entry`, `.totals`, `.query` and `.article-layout` all use
  `var(--split) minmax(0, 1fr)` and carry **no horizontal padding** — padding goes on
  their cells. That is what holds the rule and every column edge to 0.003px. Adding
  padding to any of those elements silently breaks the alignment everywhere.
- **`minmax(0, 1fr)`, never bare `1fr`.** A `1fr` track has an automatic min-content
  floor, and the `white-space: nowrap` folio label will stretch the whole grid past
  the viewport instead of overflowing it. This caused a 561px document on a 390px
  screen once already.
- **Red is red ink**, meaning an unposted entry — never emphasis. `--red` (3.24:1) is
  restricted to rules and 24px+ marks; small red type uses `--red-text` (5.77:1).
  **Every red row also carries the word for its state.** Two automated checks enforce
  this: `test_unposted_entries_never_rely_on_colour_alone` and the harness's
  `redIsNeverTheOnlyMarker`. Do not add a red row without a textual marker.

Everything is native CSS/JS. No package, no CDN, no GSAP, no Lenis. See `design-qa.md`
for why each of those was considered and declined.

### 2.2 The type system — two faces, three registers, no serif

The serif is gone. `site/assets/fonts/` holds **Archivo** (variable, `wght 100–900`,
`font-stretch 62–125%`, SIL OFL, Omnibus-Type) and **IBM Plex Mono**.

| Register | Face | Where it is allowed |
| --- | --- | --- |
| **Folio label** | Archivo `font-stretch: 68%`, `wght 700` | `.folio-label` only |
| **Statement** | Archivo `font-stretch: 100%`, `wght 500` | `.statement`, h1/h2, article h1/h2, resource-card h3 |
| **Entry** | IBM Plex Mono 400/500 | everything else — body, labels, entries, code, figures |

Rules that must survive future edits:

- **No serif, anywhere.** The harness reports `serifDeclarationsRemaining`, which must
  stay `0`. Instrument Serif and Newsreader were deleted from the repository.
- **Glyph coverage is measured, not assumed.** `document.fonts.check()` reports family
  availability, **not** glyph coverage, and it will lie about this. Compare rendered
  advance widths against a bare generic instead. The latin subsets of *both* faces
  stop short of `→` (U+2192), `↗` (U+2197) and `∞` (U+221E), so those three are
  covered by `plex-mono-symbols.woff2` — a 1.4 KB glyph-exact subset scoped by
  `unicode-range` and listed **first** in `--sans` and `--mono`. Archivo does carry
  `↑ ↓ − ∕ — –`; it does not carry the three above.
- **The folio label is ruled off the page.** `.folio-label::after` is a flexing
  hairline that runs from the end of the words, across the vertical rule, and out
  past the right edge. This makes the bleed the same structural move on every folio
  rather than an accident of how long one title happens to be — sizing alone made
  exactly one of seven labels bleed, which read as a bug.

Latin payload is **121,120 bytes across four `woff2` files** (down from ~220 KB across
six), all `font-display: swap`. There is no Plex Mono 700 — nothing resolves to it, and
every heavy weight comes from Archivo's single variable file. Licences sit beside them
as `OFL-archivo.txt` and `OFL-plex-mono.txt` — keep them there; OFL redistribution
requires it.

## 3. Repository map

### Runtime / protocol

- `src/finalisma_mcp/core.py` — schema-v3 SQLite-backed domain store: agents and
  SHA-256-only actor credential records, tasks, leases, fencing tokens, pairing
  links, separate sessions, ordered events, evidence, idempotency, and audit records.
- `src/finalisma_mcp/server.py` — MCP JSON-RPC dispatcher, stdio transport,
  authenticated Streamable HTTP, pairing/session HTTP routes, origin/token gates,
  and metrics/rate-limit surfaces.
- `src/finalisma_mcp/__main__.py` — CLI entry point and actor-auth policy resolver:
  HTTP required / stdio trusted in `auto`, explicit loopback-only HTTP trust with a
  warning, and rejection of non-loopback trust.
- `scripts/finalisma-mcp.py` — no-install launcher that adds `src/` to the import
  path and starts the MCP server.
- `scripts/finalisma-smoke.py` — real in-process protocol smoke: pairing preview,
  join, task claim, evidence verification, and completion.
- `.finalisma/state.db` — ignored project-local SQLite state currently present on
  this machine. Do not commit it. Do not delete it casually; it may contain local
  runtime state. The smoke script uses a temporary directory.

### Website / launch surface

- `site/index.html` — landing page: seven folios, semantic markup for the spread.
- `site/styles.css` — the ledger system: tokens, the fixed ground, the 37/63 grid,
  entries, the reconciliation spread, the scroll-driven layer, and article pages.
- `site/app.js` — the reconciliation controller (posts entries, drives `data-balance`
  and `data-demo-step`), clipboard fallback, reveal observer, mobile navigation.
- `site/design-target.svg` — local visual target used by the design QA comparison.
  Re-authored to DOUBLE ENTRY; it loads the real webfonts by relative path.
- `site/llms.txt` — concise AI-readable product facts.
- `site/blog/` — focused field-note articles. Each carries `.ledger-ground` too.
- `site/assets/og-card.svg` — the social card, and the single source of truth for it.
- `site/assets/og-card.png` — rendered from that SVG at exactly 1200×630.
- `scripts/finalisma-site.py` — dependency-free static server for the launch site.
- `scripts/capture-site-qa.cjs` — Playwright capture and interaction harness.
- `scripts/render-og-card.cjs` — renders `og-card.svg` → `og-card.png` via Playwright
  and verifies the PNG header is 1200×630. This replaced a PowerShell script that
  redrew the card from scratch in System.Drawing; that was a second source of truth
  which could not use the webfonts and would have drifted silently. Do not restore it.
- `artifacts/design-qa/` — generated screenshots; keep them when they document a
  verified design state, and regenerate them after frontend changes.

### Product / launch documentation

- `README.md` — installation, MCP config, first handshake, link-first pairing,
  HTTP mode, development checks, and cleanup.
- `docs/PROTOCOL.md` — Finalisma A2A envelope, lifecycle, evidence gate, and MCP
  compatibility boundary.
- `docs/PRODUCTION_PROTOCOL.md` — pairing/session security and reconnect model.
- `docs/PAIRING_UX.md` — the intended two-minute link-first pairing experience.
- `docs/SECURITY_GATES.md` — what is safe for single-node preview versus hosted
  multi-instance traffic.
- `docs/GO_LIVE.md` — local run, vertical slice, launch checklist, and release gate.
- `docs/YC_READINESS.md` — investor review, wedge, activation/retention metrics,
  and remaining production requirements.
- `docs/YC_APPLICATION.md` — application draft; it still has `[FILL]` placeholders
  that must be replaced with measured facts before submission.
- `docs/PRODUCT_HUNT.md` — launch listing, maker comment, demo order, and checklist.
- `docs/DEMO_VIDEO.md` — reproducible recorded-proof pipeline, factual boundary,
  storyboard, and release checks. The public MP4, WebM, poster, captions, and
  redacted transcript live under `site/assets/`.
- `scripts/build-site-release.py` — final deployment materializer. It requires a
  real HTTPS origin and founder-owned contact, writes only under this project,
  and produces absolute share metadata, sitemap, robots declaration, contact CTA,
  and media-hash manifest without putting fake values into `site/`.
- `docs/AI_READABILITY.md` — canonical facts and content rules. Never imply the
  browser simulation is a live remote session.
- `examples/dual-agent.md` — model/provider boundary and MCP install examples.
- `examples/pairing-link.md` — link-first pairing walkthrough.
- `examples/mcp.json` — example MCP configuration shape.

## 4. Frontend implementation details

The reconciliation spread is the `#reconciliation` folio in `site/index.html`.
Important hooks:

```text
[data-scroll-story]        tall track; also carries data-demo-step and data-balance
[data-scroll-viewport]     sticky inner viewport
[data-demo-events]         the <ol> of entries
[data-entry]               one entry row; .is-posted is the only state class
.entry-mark                the row's own rule, drawn by the scroll-driven layer
[data-demo-session]        polite live region (account status)
[data-balance-label]       "Out of balance" / "Balanced"
[data-story-progress-label] "Posted N of 7"
[data-sim-label]           simulation disclosure (AI_READABILITY rule 3)
--header-h                 sticky offset; the single source of truth for it
--split                    37%, the position of the rule
.type-wipe                 masked reveal, piggybacks on .reveal/.is-in
```

**Every entry is in the DOM from first paint.** The page is a fully drawn ledger with
nothing posted yet; scroll and the controls only toggle `.is-posted`. Nothing is
created mid-scroll, which is what lets the scroll-driven CSS layer always have rows
to animate. Do not go back to building rows with `innerHTML`.

**The two motion layers must not fight.** JS owns colour and state; the CSS
scroll-timeline animates `transform` on `.entry-mark` only. They touch disjoint
properties on purpose — if the scroll layer ever animated `color`, it would win over
the class-based rules (animations beat normal declarations) and the manual controls
would stop changing anything in Chromium. The harness asserts the two stay in step
via `rulesTrackPosting`.

`--header-h` is read once in `app.js` and by the harness. It used to be a literal
`74` in three files, which is how they drifted. Do not re-inline it.

The mobile fix is important: `overflow-x: clip` is used instead of
`overflow-x: hidden`. In Chromium, `hidden` makes an ancestor a scroll container and
the sticky spread stops sticking. `.recon-body` deliberately has **no inner scroll
container** — one on a sticky panel swallows page scroll on touch — so the rows are
sized to fit instead, and the harness asserts `entriesFitPinnedPanel`.

Do not replace this with a wheel hijack, body scroll lock, fixed page takeover, or
layout-property animation. Preserve transforms/opacity/CSS variables and the
reduced-motion branch.

### 4.1 Verifying the scroll-driven layer

It must be checked **both ways**. Chromium supports `animation-timeline`, so the
`@supports` branch is always taken locally and the fallback is never exercised by
accident. To force it, inject:

```css
.recon-track { view-timeline-name: none !important; }
.recon-body .entry-mark { animation: none !important; transform: scaleX(1) !important; }
```

then confirm the five checkpoints still pin at `--header-h` and the account still
reaches `7 / balanced`. The reconciliation is authored twice on purpose; that is a
real maintenance cost and the reason it is checked.

### 4.2 Traps that already cost time — do not re-enter them

1. **Never reveal with `clip-path` on an element the IntersectionObserver watches.**
   Chromium factors the element's own clip into its intersection rect, so a
   `clip-path: inset(0 100% 0 0)` heading reports ~2% ratio, never crosses the
   `threshold: 0.12` in `app.js`, and stays invisible forever. The headings use
   `mask-position` instead, which leaves the intersection rect intact.
2. **`minmax(0, 1fr)`, never bare `1fr`, in any split grid.** A `1fr` track has an
   automatic min-content floor and the `nowrap` folio label stretches the whole grid;
   this produced a 561px-wide document on a 390px viewport.
3. **No horizontal padding on any element that carries the 37% split.** The rule's
   alignment depends on every such grid resolving its percentage against the same
   width. Put padding on the cells (`.debit` / `.credit` / `.entry-debit` / …).
4. **`document.fonts.check()` does not report glyph coverage.** It returned "fine"
   for `∞` on faces that do not contain it. Measure advance widths against a bare
   generic instead. See §2.2 for what is and is not covered.
5. **Write the element, not just the rule.** `.entry-mark` had complete CSS and no
   markup for a while, so every row rule was silently missing.

## 5. Native subagent model rules

The user explicitly wants native Codex child agents for Qwen, LongCat, and Mimo. The
current environment instructions define these exact model IDs:

```text
qwencloud/qwen3.8-max-preview
longcat/LongCat-2.0
opencode-go/mimo-v2.5
```

When a subagent is requested, use the native `multi_agent_v1__spawn_agent` tool with
`agent_type: "default"` and a concrete bounded task, then wait using
`multi_agent_v1__wait_agent` with the returned IDs. Do not infer availability from a
stale tool description. Never silently substitute another model. If native spawning
fails, report the exact backend error and distinguish model availability from
orchestration-tool availability.

Historical note: on 2026-07-28, two native Qwen 3.8 Max xhigh implementation lanes
were invoked for the scroll-story but stayed unresponsive and made no file changes.
A LongCat-2.0 read-only review lane was also invoked and did not return a bounded
report before shutdown. The current frontend was therefore completed and verified
locally; do not claim that Qwen or LongCat authored or signed off this pass.

For `DOCS-TRUTH-SYNC-009-SOL`, the native LongCat relaunch was rejected by the
orchestration backend after the earlier lane only partially edited documentation. The
user explicitly authorized Sol as final integrator. Do not attribute the completed
documentation sync to LongCat or infer that the model itself is unavailable.

## 6. How to run it without installing anything

No project dependency install is needed. Python must be 3.11+; the project has no
runtime dependencies in `pyproject.toml`.

### Launch the site

The site server defaults to port 4173. The currently verified live server used port
4175 because that port was already managed for this workspace:

```powershell
Get-NetTCPConnection -LocalPort 4175 -State Listen -ErrorAction SilentlyContinue
python -B .\scripts\finalisma-site.py --host 127.0.0.1 --port 4175
```

Open <http://127.0.0.1:4175/>. Check both `/` and `/blog/index.html` return 200 before
claiming the site is live. Do not start a second server if port 4175 is already
listening.

### Run the real protocol smoke

```powershell
python -B .\scripts\finalisma-smoke.py
```

Expected result includes `status: ok`, `pairing_preview: issued`,
`session_state: active`, `task_status: done`, and `evidence_passed: true`.

### Run tests and frontend QA

```powershell
python -B -m unittest discover -s tests -v
python -B .\scripts\finalisma-smoke.py
node --check .\site\app.js
node .\scripts\capture-site-qa.cjs
```

To regenerate the social card after editing `site/assets/og-card.svg`:

```powershell
node .\scripts\render-og-card.cjs
```

**Verified for the current launch pass:** 65/65 Python tests and
`evidence_passed: true` on the smoke. The last recorded DOUBLE ENTRY harness run had
`consoleErrors: []` and all 13 harness boolean checks true — `allPinned` with the five
checkpoints at exactly 64px
(`--header-h`), `reachesDone`, `startsOutOfBalance`, `rulesTrackPosting`,
`ledgerRuleAligned` (max column drift 0.003px), `redIsNeverTheOnlyMarker` (11
unposted rows, all worded), `reducedMotionStatic`, `mobileNoHorizontalOverflow`
(390 = 390) and `mobileStoryNoHorizontalOverflow`. The scroll-driven layer was also
verified with the `@supports` branch forced off (§4.1): checkpoints still pin and the
account still reaches `7 / balanced`. Blog index, article, mobile article and 404 were
captured clean with zero console errors and zero serif declarations.

The site copy and audit total now state 65 tests, with the same assertion in
`tests/test_site.py`. **That number is a measured fact stated on the page** — if you
add or remove a test, update `site/index.html` (the microcopy in folio 00 and the total
in folio 04) and the assertion in `tests/test_site.py`. Do not confuse this test count
with the permanent 37/63 visual split.

The old `pointerTiltChanged` / `pointerTiltReset` checks are gone: the hero 3D tilt
object they measured was deleted with the mockups.

The Playwright runtime used by the capture script is already available in the bundled
Codex runtime. Do not install browsers or add a Node package just to run the harness.

### Performance-goal result

The locked single-node evaluator is `scripts/finalisma_performance_gate.py`; its
baseline and OMX ledger live under
`.omx/goals/performance/single-node-coordinator-envelope/`. The verified result is
1,265.771 ms -> 59.314 ms weighted median (95.31% faster), with matching semantic
digests, all scenario p95 values improved, 65 tests passing, smoke passing, and no
raw credential in evaluator output. Read `docs/PERFORMANCE.md` before changing the
harness, baseline, connection pooling, routing query, or session cursor path.

`FinalismaStore` now owns bounded read/write connection pools. Long-lived callers
must call `close()` or use the store as a context manager. The CLI and operator
scripts close it during shutdown. Do not remove that lifecycle handling: Windows
will keep temporary SQLite files locked while pooled connections remain open.

## 7. Security / product truth boundaries

- Pairing links are one-use and preview-before-consent.
- Schema v3 issues a new identity's `actor_token` once and stores only its SHA-256
  hash. Existing identities and protected team/work-plane calls require the bound
  token. A newly invited identity bootstraps through pairing; an existing invitee
  must prove its current token and cannot be overwritten by an invite.
- Pairing/session tokens are opaque and stored hashed; token-bearing URL paths are
  rejected. The HTTP adapter keeps the one-time token in the URL fragment and sends
  it in the POST body. Session tokens remain separate from actor credentials.
- `finalisma_rotate_agent_credential` atomically replaces an actor token and returns
  the replacement once. A v2 database migrates to v3 without fabricated credentials;
  recover an old identity only through trusted local rotation/bootstrap or move work
  to a genuinely new paired ID.
- Task claims are atomic and receive leases/fencing tokens.
- Declared workspace scopes are enforced; evidence hashes artifacts and rejects
  scope escapes/high-confidence secret signatures.
- Completion requires every submitted check to pass.
- The server never evaluates or executes commands found in task/message payloads.
- Local stdio is the easiest route and defaults trusted in actor-auth `auto` mode.
  HTTP defaults to required actor auth, including loopback. Explicit HTTP trust is
  loopback-only and warns; non-loopback trust is rejected. A non-local bind also
  requires transport bearer auth. Read `docs/SECURITY_GATES.md` before treating it
  as hosted SaaS.
- The current storage model is durable SQLite single-node preview. It is not yet a
  multi-instance, OAuth/OIDC, distributed-rate-limit, outbox-backed hosted service.
- Model names are recorded provider routes. The host still owns credentials and
  execution; Finalisma does not run the models.

## 8. Known gaps and next work

Priority order for the next agent:

1. Do a real manual browser pass at desktop, tablet, iOS/Safari-like mobile, and
   keyboard-only interaction. The automated Chromium evidence is strong but is not a
   substitute for every browser.
2. Replace `[FILL]` placeholders in `docs/YC_APPLICATION.md` with measured design
   partner evidence, or remove the claims.
3. Test the actual MCP install in at least two real host products. Capture the exact
   config UI, first pairing time, failure cases, reconnect behavior, and unsupported
   host adapters.
4. Add the first real design-partner workflow and instrument activation, first
   evidence-gated handoff, repeat handoff, reconnect, failure, and time-to-value metrics.
5. Before public hosted traffic, implement or explicitly scope OAuth/OIDC,
   multi-tenant isolation, shared storage, distributed rate limits, outbox/retry
   behavior, retention/deletion, load/partition/reconnect tests, and operational
   alerting. Use `docs/SECURITY_GATES.md` as the gate.
6. Optional visual polish: add a real conversion destination. Do not invent an
   email capture or customer traction. (`site/assets/favicon.svg` already exists
   and is wired up.)
7. Typography is now three self-hosted OFL families (see §2.2). If a brand face
   ever replaces one of them, swap the `@font-face` src and the matching
   `--display` / `--serif` / `--mono` token — nothing else references the family
   names directly. Do not move any of them to a CDN; local assets are what keeps
   the dependency-free promise true.

Current mobile QA is clean: document width equals the 390px viewport, no horizontal
page scroll is exposed, and the overflow-offender scan reports no offenders. Keep
the rendered width and offender checks in future visual regression passes.

## 9. Workspace hygiene rules

- Work only inside `C:\Users\Wasif\Documents\Multiplayer-AI`.
- Use `apply_patch` for source edits. Do not use shell redirection or ad hoc file
  writers for code/doc changes.
- Do not install globally, modify PATH/profile/registry, create services, or add
  startup entries.
- Do not add secrets to `.env`, shell profiles, logs, task payloads, MCP JSON, or git.
- Do not delete anything outside this project. Inside the project, list generated
  artifacts before cleaning them.
- `.gitignore` already excludes Python caches, virtual environments, SQLite databases,
  and `.finalisma/`. The working tree currently appears untracked wholesale; do not
  use `git reset --hard`, `git checkout --`, or broad cleanup commands.
- Generated QA screenshots under `artifacts/design-qa/` are intentional evidence,
  not random cache. Remove only if the user explicitly wants the artifact set pruned.

## 10. Definition of done for a future change

Before handing work back:

1. State the user-facing behavior changed and the exact files.
2. Run targeted tests, then the full relevant test/smoke/capture checks.
3. Inspect the actual rendered page, not only source or build output.
4. Check mobile width, reduced motion, keyboard focus, console errors, and truthful
   copy for any UI change.
5. Report any model/subagent completion truthfully, including proxy failures.
6. Leave the server state and cleanup instructions explicit.
