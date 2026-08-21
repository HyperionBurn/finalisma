# Weft — agent handover

> Read this before changing anything. This is the shortest complete explanation of
> what this repository is, what has already been built, what is verified, and what
> must not be accidentally broken.

The website release audit below is historical. It is
[`docs/WEBSITE_COMPLETION_AUDIT_2026-07-31.md`](docs/WEBSITE_COMPLETION_AUDIT_2026-07-31.md).
Read it for dated local evidence only. Use [`docs/GO_LIVE.md`](docs/GO_LIVE.md)
and [`docs/RELEASE_EVIDENCE.md`](docs/RELEASE_EVIDENCE.md) for current local
status and the remaining deployment gates.

## 1. The product in one paragraph

Weft is a dependency-free MCP coordination layer for making two separate AI
agents behave like a governed team. The product promise is:

> Weft is designed for two MCP-capable hosts with compatible stdio or
> Streamable HTTP integration. They can share a scoped task, messages, leases,
> ordered events, and evidence without sharing provider credentials or conversation
> history. Interoperability status: one real host verified (OpenCode 1.18.13,
> `docs/INTEROP_VALIDATION_2026-08-05.md`); the HTTP, bridge, and SDK tiers have
> committed protocol-tier transcripts (`docs/INTEROP_HTTP_2026-08-05.md`,
> `docs/INTEROP_BRIDGE_2026-08-05.md`, `docs/INTEROP_SDK_2026-08-05.md`);
> host-product breadth for those tiers remains open; the protocol-tier transcript
> is landed in `docs/INTEROP_VALIDATION_2026-08-15.md`.

The first startup wedge is evidence-backed handoffs for AI-native engineering teams:
incident triage and pull-request review. One agent opens a narrowly scoped task;
the other previews/consents, claims it under a lease, works inside the declared
scope, returns evidence, and can only complete after every check passes.

Weft coordinates agents. It does not execute arbitrary shell commands from
payloads, silently substitute model providers, or mutate host settings.

## 2. Current state at handoff

The repository contains both a real local MCP server and a no-build launch site.
The launch site has since been rebuilt as **DOUBLE ENTRY** (§2.1). The scroll-story
demo survived that rebuild as the reconciliation spread in folio 03:

- The page scrolls naturally through a tall track (`.recon-track`).
- The pinned viewport (`.recon-sticky`) stays below the site header, at the offset
  declared once as `--header-h` (currently 58px).
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

### 2.1 The design — FIELD NOTES

The live design is **FIELD NOTES**: a magazine feature, not a landing template. The page
alternates oxblood cover bands with paper-stone article bands, so it has rhythm down its length
instead of one flat ground. Two roles are carried through the whole piece in colour and italic:
**ASSERT** (crimson/coral) and **PROVE** (ochre). They appear in the headline, the sidebar, the
figure and the diagram, and they never mean anything else.

**Superseded designs — do not restore any of them:** control-room broadsheet (deleted 2026-07-31)
→ DOUBLE ENTRY ledger (deleted after FIELD NOTES shipped) → FIELD NOTES (current). DOUBLE ENTRY
failed for reasons recorded in the `site/styles.css` header: body copy set in monospace (reads as
terminal output), a label/value table as the core device (a spreadsheet, and the anatomy of a
slide), a palette built only by subtraction, and two type sizes with a void between them. The
serif is not an accident here — it is the point. **Never "fix" serif usage.**

Load-bearing rules:

- **12-column grid** (`--shell: 1320px`, `--gut`, `--pad` as clamp tokens). `.cover-grid`,
  `.feature-grid`, `.spec-grid`, `.cohort-grid`, `.article-layout` all use
  `repeat(12, 1fr)` with `minmax(0, 1fr)` semantics where content must not stretch the grid —
  a `1fr` track has an automatic min-content floor that breaks the layout on narrow screens.
- **No horizontal padding on any element that carries a grid column.** Padding goes on the
  cells (`.cover-main`, `.spec-copy`, `.article-layout > article`, …). Adding padding to a
  grid container silently breaks column alignment everywhere.
- **Colour is never the only marker.** ASSERT red and PROVE ochre carry meaning, but every
  colour-coded row also carries a word for its state. Two automated checks enforce this:
  `test_unposted_entries_never_rely_on_colour_alone` and the harness's
  `redIsNeverTheOnlyMarker`. Do not add a coloured row without a textual marker.
- **The proof plate is fail-safe by default.** `.plate-stage` and `.plate-controls` are
  `display: none` until `proof-engine.js` mounts and sets `.plate.is-ready`. Without
  JavaScript — or with a broken engine — the reader gets the written sequence
  (`.plate-static`) instead of a blank rectangle. Never invert this.
- Everything is native CSS/JS. No package, no CDN, no GSAP, no Lenis. See `design-qa.md`.

### 2.2 The type system — two faces, three registers

`site/assets/fonts/` holds three self-hosted OFL families (no CDN, no runtime network
dependency):

| Register | Face | Where it is allowed |
| --- | --- | --- |
| **Display / labels** | Big Shoulders Display (condensed grotesque, `wght 400–900`) | masthead, folio labels, kickers, small caps, figures, buttons. Never body copy. |
| **Text / display serif** | Fraunces (variable `wght 300–900`, optical-size axis, true italic) | headline, standfirst, body copy, h1/h2, pullquotes, article text |
| **Code / data** | `ui-monospace` stack | code blocks only |

The headline is the point: Fraunces breaks roman into italic mid-phrase (`.headline .l2`,
`.headline .l3`) to change voice, not just line. The two colour roles also travel as italic:
`.term-assert`, `.term-prove`, `.kicker-prove`.

Rules that must survive future edits:

- **Keep the webfonts local and the licences beside them.** `OFL-fraunces.txt` (and the
  matching Big Shoulders licence) must stay in `site/assets/fonts/`; OFL redistribution
  requires it. Do not move any family to a CDN.
- **Glyph coverage is measured, not assumed.** `document.fonts.check()` reports family
  availability, **not** glyph coverage, and it will lie about this. Compare rendered advance
  widths against a bare generic instead.
- **`--header-h` is the single source of truth** for the sticky masthead offset (currently
  58px). It is read once in `app.js` and by the harness. Do not re-inline it.
- **`overflow-x: clip`, never `overflow-x: hidden`** — `hidden` makes an ancestor a scroll
  container and sticky positioning stops working in Chromium.

## 3. Repository map

### Runtime / protocol

- `src/weft_mcp/core.py` — schema-v3 SQLite-backed domain store: agents and
  SHA-256-only actor credential records, tasks, leases, fencing tokens, pairing
  links, separate sessions, ordered events, evidence, idempotency, and audit records.
- `src/weft_mcp/server.py` — MCP JSON-RPC dispatcher, stdio transport,
  authenticated Streamable HTTP, pairing/session HTTP routes, origin/token gates,
  and metrics/rate-limit surfaces.
- `src/weft_mcp/__main__.py` — CLI entry point and actor-auth policy resolver:
  HTTP required / stdio trusted in `auto`, explicit loopback-only HTTP trust with a
  warning, and rejection of non-loopback trust.
- `src/weft_mcp/tenancy.py` — org/membership boundary layer: org CRUD, member
  roles, SHA-256 actor-key derivation, and scope enforcement (`assert_scope`). Mounted
  behind `org_*` MCP tools (2026-08-05).
- `src/weft_mcp/roster.py` — N-way roster: create/join/leave, capabilities,
  named groups, one-use roster links, `route_targets` expansion (agent / group /
  `*` / list), and `build_envelope_v2`. Mounted behind `roster_*` tools.
- `src/weft_mcp/outbox.py` — durable per-recipient outbox: fan-out enqueue,
  atomic claim, exponential backoff retry, DLQ, restart crash-recovery. Mounted
  behind `outbox_*` tools.
- `src/weft_mcp/bridge.py` — universal adapters for non-MCP hosts:
  `WebhookBridge` (HMAC-signed POST, fails closed without the real signing secret),
  `PollingBridge` (at-least-once delivery until acknowledgement with a
  persisted monotonic cursor), `ClipboardBridge` (one-shot
  bootstrap snippet). Mounted behind `bridge_*` tools.
- `src/weft_mcp/metrics_activation.py` — local-first activation funnel:
  `link_created → link_previewed → link_accepted → first_task_claimed →
  first_evidence_verified` with time-to-first-verified-handoff, retention, and
  handoffs-per-workspace. Mounted behind `metrics_*` tools. No external
  analytics vendor, no PII.
- `src/weft_mcp/room.py` — the Wave E Room product object: one multi-use link admits
  N agents (bounded by a cap) with preview-before-consent; ordered event log with per-member
  cursors; addressing (unicast / group / broadcast) with durable outbox delivery receipts;
  presence from heartbeats. Composes `roster`/`outbox`/`core`; owns `room_*` tables. Mounted
  behind 12 `room_*` tools. Design: `docs/ROOMS_DESIGN.md`.
- `src/weft_sdk/` — official stdlib-only Python client (`WeftClient`):
  typed results, structured errors, token hygiene (never in repr/logs), exponential
  backoff retry with idempotency keys.
- `src/weft_cloud/` — the Wave F hosted-service plane. `storage.py` defines the
  `StorageBackend` ABC (transport-engine-agnostic) with a `SqliteWalBackend` (stdlib SQLite-WAL).
  `tenancy.py` is structural isolation: `TenantContext.require_tenant()` guard + required
  `tenant_id` on every storage method. `migrations.py` is forward-only/idempotent and upgrades a
  real v3 coordinator DB in place (additive only, never touches agent_credentials).
  `quotas.py`/`rate_limit.py` are plan-driven seams (Wave I adds billing). `mcp.py` is the
  hosted MCP endpoint at `POST /mcp` — authenticated with cloud sessions, tenant-confined,
  exposing the 14 room tools over `CloudRoomService`; `service.py` routes `/mcp` to it
   (Design: `docs/CLOUD_SPINE_DESIGN.md`, `docs/HOSTED_MCP_DESIGN.md`).
   `cloud_outbox` (the HOSTED delivery outbox — distinct from the email
   `cloud_identity_outbox`) has a full completion lifecycle: migration `cloud_011`
   adds `claimed_at`/`claimed_by`/`last_error`/`dispatched_at`, the backend exposes
   lease-aware `claim_due_outbox` + `mark_outbox_delivered/retry/dead`, and
   `delivery_worker.py` drains it with a pluggable `Deliverer` (lease reclaim,
   retry with backoff, terminal `delivered`, dead-letter after max attempts).
  This plane MAY take pinned deps; v1 uses none (stdlib).
- `src/weft_cloud/identity/` — the Wave G identity plane (stdlib only). `accounts.py`
  (scrypt password hashing, per-user salt, constant-time compare, timing-invariant unknown-email
  auth, single-use verify/reset tokens), `sessions.py` (opaque `fss_` tokens, SHA-256 at rest,
  expiry, revoke / revoke-all, rotation on role change), `orgs.py` (membership + owner/admin/member;
  org IS a tenant — no second boundary), `invites.py` (expiring single-use role-scoped `fiv_` tokens,
  email-locked, no self-escalation), `mailer.py` (`Mailer` ABC + `LocalOutboxMailer` → outbox table),
  `tokens.py` (AuthError + CSPRNG token gen/hash), `context.py` (`SessionContext.require_role` =
  layer 1; `require_db_role` re-derives the actor's role from `cloud_identity_members` = layer 2,
  so a forged SessionContext still cannot act above its DB role). Migrations `cloud_002..cloud_006`
  add accounts/sessions/members/invites/outbox. 58 Wave G integration tests drive the real API.
  Design: `docs/IDENTITY_DESIGN.md`.
- `scripts/weft-mcp.py` — no-install launcher that adds `src/` to the import
  path and starts the MCP server.
- `scripts/weft-smoke.py` — real in-process protocol smoke: pairing preview,
  join, task claim, evidence verification, and completion.
- `scripts/weft_performance_gate.py` — locked same-machine evaluator. Re-baselined
  2026-08-05 after the harness gained roster/tenancy scenarios; original 1,265.771 ms
  → 59.314 ms / 95.31% is preserved in the baseline `history` array and
  `docs/PERFORMANCE.md`. A force re-capture is a regression guard (target 0), not an
  improvement proof.
- `.weft/state.db` — ignored project-local SQLite state currently present on
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
- `scripts/weft-site.py` — dependency-free static server for the launch site.
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
- `docs/PROTOCOL.md` — Weft A2A envelope, lifecycle, evidence gate, and MCP
  compatibility boundary.
- `docs/PRODUCTION_PROTOCOL.md` — pairing/session security and reconnect model.
- `docs/PAIRING_UX.md` — the intended two-minute link-first pairing experience.
- `docs/SECURITY_GATES.md` — what is safe for single-node preview versus hosted
  multi-instance traffic.
- `docs/GO_LIVE.md` — local run, vertical slice, launch checklist, and release gate.
- `docs/DEPLOY.md` — container runbook for the cloud service: environment
  configuration, Dockerfile/compose build and verify, volume backup/restore,
  rollback, and the single-instance SQLite-WAL constraint (never scale
  horizontally; durability comes from the volume, not replicas).
- `Dockerfile`, `.dockerignore`, `compose.yaml` — the cloud service image
  (stdlib-only, non-root, `/data` volume) and the one-command local stack with
  a persistent named volume for the database.
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

This environment is opencode, not Codex. There is no `multi_agent_v1__spawn_agent` /
`multi_agent_v1__wait_agent`; those tools do not exist here and never will. The correct
mechanism is the `task` tool with `subagent_type: "longcat-2.0"`, backed by the global agent
definition `~/.config/opencode/agents/longcat-2.0.md`.

Model-pinning lessons recorded 2026-08-05 (do not re-learn them the hard way):

- A subagent inherits the parent session's model unless its agent definition pins
  `model: "provider/model-id"`. The global `longcat-2.0.md` agent pins
  `model: LongCat/LongCat-2.0`.
- opencode reads agent config at session start. It does **not** hot-reload mid-session.
  After editing `opencode.json`/agent files you must restart the session for changes to take
  effect.
- Project-scoped config (`opencode.json` in a project root) only loads when the session is
  rooted at that project. A session rooted at `C:\Users\Wasif` will not see worktree config.
- Task-permission deny on an agent name removes it from the parent's task tool entirely but
  still lets the user summon it via `@mention`. Use this for user-invocation-only lanes.
- When a lane is requested, use the `task` tool with a concrete bounded task. If spawning
  fails, report the exact backend error and distinguish model availability from
  orchestration-tool availability. Never silently substitute another model.

Historical note: on 2026-07-28, two native Qwen 3.8 Max xhigh implementation lanes were
invoked for the scroll-story but stayed unresponsive and made no file changes. A LongCat-2.0
read-only review lane was also invoked and did not return a bounded report before shutdown.
The current frontend was therefore completed and verified locally; do not claim that Qwen or
LongCat authored or signed off this pass. For `DOCS-TRUTH-SYNC-009-SOL`, the native LongCat
relaunch was rejected by the orchestration backend after the earlier lane only partially
edited documentation. The user explicitly authorized Sol as final integrator. Do not
attribute the completed documentation sync to LongCat or infer that the model itself is
unavailable.

## 6. How to run it without installing anything

No project dependency install is needed. Python must be 3.11+; the project has no
runtime dependencies in `pyproject.toml`.

### Launch the site

The site server defaults to port 4173. Use port 4175 as an alternate local port
when the default is occupied. The port is not a claim that a server is listening:

```powershell
Get-NetTCPConnection -LocalPort 4175 -State Listen -ErrorAction SilentlyContinue
python -B .\scripts\weft-site.py --host 127.0.0.1 --port 4175
```

Open <http://127.0.0.1:4175/> after the command starts. Check both `/` and
`/blog/index.html` return 200 before claiming the site is live. Do not start a
second server if port 4175 is already listening.

### Run the real protocol smoke

```powershell
python -B .\scripts\weft-smoke.py
```

Expected result includes `status: ok`, `pairing_preview: issued`,
`session_state: active`, `task_status: done`, and `evidence_passed: true`.

### Run tests and frontend QA

```powershell
python -B -m unittest discover -s tests -v
python -B .\scripts\weft-smoke.py
node --check .\site\app.js
node .\scripts\capture-site-qa.cjs
```

To regenerate the social card after editing `site/assets/og-card.svg`:

```powershell
node .\scripts\render-og-card.cjs
```

**Historical launch snapshot (2026-08-15, `feature/product-perfect`):** 960/960 Python tests and
`evidence_passed: true` on the smoke. The last recorded `capture-site-qa.cjs`
run on file (with `consoleErrors: []` and all harness booleans true —
`allPinned` with the five checkpoints at exactly 64px (`--header-h`),
`reachesDone`, `startsOutOfBalance`, `rulesTrackPosting`, `ledgerRuleAligned`
(max column drift 0.003px), `redIsNeverTheOnlyMarker` (11 unposted rows, all
worded), `reducedMotionStatic`, `mobileNoHorizontalOverflow` (390 = 390) and
`mobileStoryNoHorizontalOverflow`) predates the FIELD NOTES rebuild and is
kept as historical evidence, not a current pass. A fresh harness capture
against the current FIELD NOTES tree is required before those booleans can
be re-claimed. The scroll-driven layer's `@supports`-forced-off check (§4.1)
belongs to the same historical run.

The published test count is guarded automatically by
`tests/test_site.py::TestCountSyncTests`: it discovers the live count with the
same loader and pattern as `unittest discover -s tests` and fails if any
published instance in `docs/` or `site/` claims a count ABOVE the live count
(the guard protects against deleted tests; adding tests never invalidates a
published number). The latest local count is **1236 discovered, 1235 passed,
1 skipped**, measured 2026-08-20 and recorded in `docs/RELEASE_EVIDENCE.md`.
If you delete tests, that guard will tell you what to update. Do not
hand-maintain the number, and do not confuse the test count with the permanent
37/63 visual split.

The old `pointerTiltChanged` / `pointerTiltReset` checks are gone: the hero 3D tilt
object they measured was deleted with the mockups.

The Playwright runtime used by the capture script is already available in the bundled
Codex runtime. Do not install browsers or add a Node package just to run the harness.

### Performance-goal result

The locked single-node evaluator is `scripts/weft_performance_gate.py`; its
baseline and OMX ledger live under
`.omx/goals/performance/single-node-coordinator-envelope/`. The current reference
artifact is 72.221 ms; the latest complete local gate measured 137.404 ms /
155.381 ms p95 and failed its timing guards while its quality sub-gates passed.
The 2026-08-14 single-trial captures (~144.8 ms, ~68.8 ms, ~145.0 ms weighted
medians against the same reference) show run-to-run variance of a size that is
host load, not code; the gate remains red pending a controlled idle-host
rerun. The older ~68.8 ms and 1,265.771 ms -> 59.314 ms (95.31%) results are
historical evidence preserved in the baseline `history` array. The gate
requires the current suite recorded in `docs/RELEASE_EVIDENCE.md` to pass,
smoke passing, and no raw credential in evaluator output. Read
`docs/PERFORMANCE.md` (the owner of
every performance number and its provenance) before changing the harness,
baseline, connection pooling, routing query, or session cursor path.

`WeftStore` now owns bounded read/write connection pools. Long-lived callers
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
- `rotate_agent_credential` atomically replaces an actor token and returns
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
- **Webhook signing fails closed.** `WebhookBridge.deliver` raises
  `signing_secret_required` unless the caller supplies the real signing secret; the
  stored SHA-256 hash is never used as a live HMAC key (HIGH-1 fix, 2026-08-05).
- **SDK error bodies are redacted.** `WeftClient` never embeds a coordinator
  response body into a raised exception — only `{status}` is attached, so a body that
  echoes a token cannot land in caller logs (HIGH-2 fix, 2026-08-05).
- **Bridge calls require actor auth.** Every `bridge_*` tool validates the
  caller's `actor_token` (`actor_auth_invalid` on failure); webhook secrets and
  bootstrap nonces are stored hashed / one-use and never returned.
- **Tenancy scope is negative-tested.** `org_assert_scope` raises
  `tenancy_scope_forbidden` for non-members and for cross-tenant key mismatches; org
  isolation is enforced by negative integration tests.
- **Outbox delivery is durable and idempotent.** Fan-out entries are keyed
  `(envelope_id, team, recipient)`; crash recovery resets stranded `in_flight` rows;
  DLQ holds entries past max attempts. No raw secret enters `payload_json`.
- **Activation metrics carry no PII.** `metrics_event` rejects PII-bearing
  metadata keys at the top level; events carry team/agent ids and caller-controlled
  metadata only.
- **Room links are multi-use up to a cap — governed, not anonymous.** A room link
  (`room_links`) admits N agents up to the room cap; a leaked link grants at most `cap`
  attributable memberships, never read access by itself. Compensating controls (Wave E,
  `docs/ROOMS_DESIGN.md` §3): per-join consent must be the literal boolean `true`
  (`consent_required` otherwise); every join binds an `agent_id` + actor credential and the
  link cannot overwrite an existing identity (`actor_auth_invalid`); cap enforcement is
  atomic under `BEGIN IMMEDIATE` (`room_full`); expiry (`link_expired`) and revocation
  (`link_revoked`) are checked on every join; only the SHA-256 of the link token is stored.
  The existing one-use two-party pairing link is unchanged — rooms are additive.
- **Room reads and mutations are member-only.** Every `room_*` tool requires an
  `actor_token` bound to a member; non-members get `member_required`, and cross-room access
  is refused. Ordered room events replay from per-member cursors with at-least-once delivery
  and monotonic MAX acks (`UNIQUE(room_id, seq)`, `room_cursors` PK `(room_id, agent_id)`).
- **Cloud tenancy is structural, not a convention (Wave F).** Every `StorageBackend` method that
  touches tenant data takes `tenant_id` as a required positional parameter (a missing one raises
  `TypeError`), and the backend scopes every query `WHERE tenant_id = ?`. `TenantContext` is
  constructed once per authenticated request and its `require_tenant()` rejects a wrong tenant
  before any backend call. There is no cross-tenant read path and no `list_all_tenants`.
  The negative test suite (`tests/test_tenancy_negative.py`) proves tenant B cannot read/list/
  address/enumerate tenant A across rooms, events, counters, outbox, and audit.
- **Cloud migrations are additive-only.** `apply_migrations` upgrades a real v3 coordinator DB in
  place, creating `cloud_*` and `schema_migrations` tables only; `agent_credentials` is never
  touched and no credentials are fabricated. Rollback is a pre-upgrade backup (no down-migrations).
- **Cloud quotas/rate limits are plan-driven and atomic.** Limits resolve from the tenant's plan
  (`PLANS`), never hardcoded; quota checks and mutations run in one transaction. Wave I swaps in
  real billing without touching enforcement.
- **Identity secrets are hashed, never stored/logged (Wave G).** Passwords are scrypt-hashed with a
  per-user 32-byte salt and compared with `hmac.compare_digest`; session (`fss_`), verification
  (`fvt_`), reset (`frt_`), and invite (`fiv_`) tokens are CSPRNG-generated, returned once, and only
  their SHA-256 digests are persisted. Unknown-email auth runs the same scrypt cost against a dummy
  hash (no timing-based enumeration); reset revokes ALL of the account's sessions in the same
  transaction. There are zero `print`/`logging` calls in the identity plane, and errors/responses
  never carry a raw secret (`tests/test_identity_negatives.py` #26-#28 assert this).
- **Roles are enforced with defence in depth (Wave G).** Layer 1: `SessionContext.require_role` at
  the service boundary (role comes from the DB-issued session, never an argument). Layer 2:
  `context.require_db_role` re-derives the actor's role from `cloud_identity_members` inside each
  gated orgs/invites operation, so a hand-forged `SessionContext(role="owner")` is still refused
  (proven by `tests/test_identity_invites.py::test_fabricated_ctx_role_cannot_create_invite`).
  Service methods take `ctx` as a required positional arg and raise `TypeError` for a non-context
  value — there is no `role` parameter to fabricate.
- **Invites are single-use, role-scoped, and cannot self-escalate (Wave G).** An invite's granted
  role is EXACTLY the invite row's role (`admin`/`member`, never `owner`); the accepter never
  supplies a role. Wrong email → `invite_mismatch`, double redeem → `invite_consumed` (atomic
  conditional UPDATE), expired/unknown → `invite_expired` (uniform, not a token oracle). The
  membership lands in the invite's own tenant — no cross-org redirect.
- **Admin can never mint an owner (2026-08-15, `d344ebe`).** `add_member` with
  `role: "owner"` requires an owner caller — `ctx.require_role("owner")` (layer 1)
  AND `require_db_role(..., "owner")` (layer 2) — mirroring `set_role`; an admin
  cannot create an owner who could then delete the org or remove the admin.
- **Agent-key signout revokes truthfully (2026-08-15, `d344ebe`).** The shared
  signout funnel checks the presented credential type: for an `agk_` bearer the
  credential IS the key, so signout revokes the key itself (and frees its room
  seats via `release_agent_key_seats_in_tx`) instead of reporting a no-op.
- **Hosted REST refuses malformed cursors with 400s, not 500s (2026-08-15, `1e0aa5a`).**
  Non-integer / negative / beyond-head `after_seq` → 400 `invalid_argument` /
  `invalid_cursor`; negative `seq` on ack → 400 `invalid_cursor`. Caller error
  is never surfaced as a server fault.
- **The SDK drives all 14 hosted room tools in hosted mode without identity
  arguments (2026-08-15, `c9f4e0e`).** When an `agk_`/`fss_` bearer is supplied,
  the client strips `team_id`/`agent_id`/`actor_token` from every tool call —
  the hosted dispatcher derives identity from the credential and rejects
  client-supplied identity — and surfaces HTTP 429 as a structured
  `rate_limited` error carrying `retry_after`.
- **Coordinator (`weft_mcp`) stays dependency-free forever.** The cloud plane is the only
  place pinned dependencies may land, and v1 adds none (stdlib SQLite-WAL).
- The current storage model is durable SQLite single-node preview. It is not yet a
  multi-instance, OAuth/OIDC, distributed-rate-limit, outbox-backed hosted service.
- Model names are recorded provider routes. The host still owns credentials and
  execution; Weft does not run the models.

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
4. Add the first real design-partner workflow. Extend the automatic activation
   instrumentation for repeat handoff, reconnect, failure, and time-to-value metrics.
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
8. The cloud service (`src/weft_cloud/service.py`) is containerised
   (`Dockerfile` + `compose.yaml` + `docs/DEPLOY.md`) as a **single-instance**
   SQLite-WAL deployment: one writer, one persistent disk, no horizontal
   scaling. The image build itself is untested until run on a machine with
   Docker installed; `docs/DEPLOY.md` records the exact verification status.
   Making it a real multi-node service is a storage-layer change.
9. A hosted MCP endpoint exists in source at `POST /mcp` on `weft-cloud`
   (`src/weft_cloud/mcp.py`, `docs/HOSTED_MCP_DESIGN.md`): authenticated with
   cloud sessions, tenant-confined, exposing the 14 room tools over
   `CloudRoomService` (the same store `/v1` uses). The **SDK now drives all
   14 hosted tools** (current surface — `room_list`, `room_close`,
   `room_wait`, `room_event_log`,
   `room_remove_member` added) and REST has `/v1/rooms/receipts` +
   `/v1/rooms/remove_member` parity with 400-not-500 cursor errors
   (`1e0aa5a`). What remains, in priority order:
   a. host-product breadth beyond the historical OpenCode 1.18.13 run — the
      HTTP/bridge/SDK protocol-tier transcripts are committed, but current
      third-party host runs remain the open item per `docs/PRODUCT_ROADMAP.md` §3;
   b. scale proof at 10/50 agents (in flight in a separate worktree — do not
      claim numbers until it lands with evidence);
   c. a controlled idle-host rerun of the performance gate, which is red
      under host-load noise (`docs/PERFORMANCE.md` owns the numbers).

Current mobile QA is clean: document width equals the 390px viewport, no horizontal
page scroll is exposed, and the overflow-offender scan reports no offenders. Keep
the rendered width and offender checks in future visual regression passes.

## 9. Workspace hygiene rules

- Work only inside the current checkout `C:\Users\Wasif\Documents\Multiplayer-AI-integration`.
  The merged base is `main` at `fd05155`; consult Git for the current feature
  branch. The latest local test evidence is in `docs/RELEASE_EVIDENCE.md`; do not
  infer merge or deployment status from it.
  `C:\Users\Wasif\Documents\Multiplayer-AI-isolated` (branch `isolated`) and
  `C:\Users\Wasif\Documents\Multiplayer-AI` (branch `master`) are **separate
  worktrees** with their own work; never read from or write to them from a
  lane in this one.
- Use the opencode `read`/`write`/`edit` tools for source changes. There is no `apply_patch`
  tool in this environment (that is Codex). Do not use shell redirection or ad hoc file
  writers for code/doc changes.
- If your shell cwd is `C:\Users\Wasif`, every command must pass
  `workdir = C:\Users\Wasif\Documents\Multiplayer-AI-integration` and every file path must be
  absolute under that worktree. Do not redirect work into the retired
  `Multiplayer-AI-perfect` checkout.
- The `AGENTS.md` in this worktree is a stale copy of the isolated worktree's
  rules (it names branch `isolated`); its product facts pre-date the
  2026-08-15 closes. Verify any of its claims against this tree before
  acting on them.
- Do not install globally, modify PATH/profile/registry, create services, or add startup
  entries.
- Do not add secrets to `.env`, shell profiles, logs, task payloads, MCP JSON, or git.
- Do not delete anything outside this project. Inside the project, list generated artifacts
  before cleaning them.
- Never `git add -A` — it is exactly what produced the broken initial snapshot (missing
  `site/proof-engine.js` and `site/docs/managed-pilot.html`). Stage explicit paths. Do not
  commit from a lane; the orchestrator commits, one commit per lane. Never run
  `git reset --hard`, `git checkout --`, or `git clean` in either worktree.
- Generated QA screenshots under `artifacts/design-qa/` are intentional evidence, not
  random cache. Remove only if the user explicitly wants the artifact set pruned.

## 10. Definition of done for a future change

Before handing work back:

1. State the user-facing behavior changed and the exact files.
2. Run targeted tests, then the full relevant test/smoke/capture checks.
3. Inspect the actual rendered page, not only source or build output.
4. Check mobile width, reduced motion, keyboard focus, console errors, and truthful
   copy for any UI change.
5. Report any model/subagent completion truthfully, including proxy failures.
6. Leave the server state and cleanup instructions explicit.
