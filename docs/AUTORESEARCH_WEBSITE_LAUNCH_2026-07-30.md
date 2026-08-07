# Weft website launch autoresearch

Date: 2026-07-30  
Codex goal: `go all in on the website`  
Research perspective: a first-time engineering buyer deciding in 90 seconds whether this is real, safe, and worth piloting.

## Research question

Can the Weft website turn a skeptical visitor into a qualified design-partner action while preserving the DOUBLE ENTRY visual system and never claiming host compatibility that the repository has not proved?

The prior product research selected one wedge: a bounded, non-production incident handoff between two agent hosts. This pass treats the website itself as the product experiment. Visual novelty counts only when it improves comprehension, trust, memory, or action.

## Professor-critic rubric

PASS requires every dimension below:

1. A visitor can state the job, user, and boundary from the first fold.
2. Documented compatibility and live Weft validation are numerically distinct.
3. The desktop reconciliation interaction tells the product story through scroll and manual control.
4. Mobile uses a reliable touch interaction at standard and short heights.
5. The primary CTA is gated by the story state and visible in the standard mobile fold.
6. The conversion action works without inventing a backend or transmitting data unexpectedly.
7. The static bundle owns its guides, articles, license, 404, and internal links.
8. No-JavaScript, reduced-motion, keyboard, live-region, and overflow behavior are verified.
9. A cold visitor can watch a real coordinator run without confusing deterministic host fixtures for live host validation.
10. Fresh protocol tests and rendered-browser checks pass with no request or console errors.
11. The human QA report and deterministic critic agree on PASS, with external launch inputs named separately.

## Baseline diagnosis

### P0 truth risk

The original primary language described a “verified agent handoff layer” even though the current matrix contains nine documented MCP paths and zero fresh Weft host runs. A YC partner or senior buyer would discover that mismatch immediately and discount every later claim.

### P1 conversion risk

High-intent actions opened repository Markdown. There was no design-partner offer, fit criteria, price hypothesis, or application flow. Public visitors had no next action connected to the product wedge.

### P1 mobile risk

The announcement became two stacked rows above a sticky header. The 390px first fold spent too much height on chrome, and short viewports tried to reproduce a 340vh desktop sticky story with insufficient vertical budget.

### P1 interaction risk

The balanced-state CTA carried `hidden`, but a later `.story-next { display: inline-block }` rule overrode the browser's hidden styling. The CTA appeared before the account balanced.

### P1 deployment risk

The development server made repository Markdown routes appear valid, but a normal static deployment contained no matching files under `site/`. The website was not actually self-contained.

### P2 resilience risk

Reveal classes started hidden, so blocked or disabled JavaScript could erase content. Copy feedback was visual only. The mobile navigation changed visibility but did not use an inert modal boundary.

## Tested hypotheses

### H1: lead with one incident, not universal interoperability

Result: supported. “One incident. Two agents. One account of what happened.” gives a concrete trigger and maps directly to the ledger metaphor. The first fold now explains the bounded transfer, the host-owned boundary, and the proof action.

### H2: publish the missing proof as part of the product story

Result: supported. The 9 documented / 0 validated account makes honesty visible rather than burying it in a FAQ. Compatibility has its own static evidence ledger and upgrade rule.

### H3: use 3D only where state changes

Result: supported. A CSS 3D reconciliation plane starts below the surface, rises as evidence posts, and settles flat when balanced. It produces at least three distinct transforms across five scroll checkpoints, adds no dependency, and becomes flat under reduced motion and on mobile.

### H4: desktop scroll theater should become mobile tap-through

Result: supported. At 390 x 844, 390 x 667, and 320 x 568, the story is static, fully visible, has no horizontal overflow, and reaches balance through controls. The standard and short 390px folds include the primary proof CTA.

### H5: a local application builder is better than a fake lead form

Result: supported with a launch limitation. The form validates real fields and prepares a portable application through Web Share, clipboard, or download. It clearly says nothing is transmitted. Direct public lead capture still requires a founder-owned endpoint or contact destination.

### H6: a generated real-run proof is stronger than a simulated browser story alone

Result: supported. A fresh local `WeftStore` run now produces a credential-redacted public transcript and a reproducible 42-second narrative encoded as 43.04-second MP4 and WebM files. Pairing, ordered relay, cursor acknowledgement, lease and fencing ownership, artifact hashing, secret scanning, evidence gating, completion, and audit events use the real coordinator. The two host actors remain labelled deterministic fixtures in the video, poster, watch page, and transcript.

## Implemented outcome

- Rewrote hero, navigation, evidence language, FAQ, audit totals, and cohort offer.
- Added a 9/0 compatibility account and proposed Codex + Claude Code validation pair.
- Added a scroll-driven CSS 3D plane with deterministic manual controls.
- Added mobile tap-through, short-height behavior, reduced motion, and no-JavaScript resilience.
- Added inert mobile navigation, live copy feedback, and a portable design-partner application.
- Added self-contained quickstart, protocol, security, compatibility, license, 404, and field-note routes.
- Added a real custom 404 response and closed every static internal link.
- Added a first-fold recorded-proof player, MP4/WebM outputs, poster, captions, redacted transcript, and a no-install Playwright/ffmpeg regeneration pipeline.
- Regenerated the Open Graph card from the revised source.
- Expanded the launch-surface test suite and the browser QA harness.

## Evidence

- Human review: [../design-qa.md](../design-qa.md)
- Machine QA: [../artifacts/design-qa/qa-results.json](../artifacts/design-qa/qa-results.json)
- Visual comparison: [../artifacts/design-qa/comparison-desktop-1440x900.png](../artifacts/design-qa/comparison-desktop-1440x900.png)
- Recorded-proof page: [../artifacts/design-qa/implementation-demo-1440x900.png](../artifacts/design-qa/implementation-demo-1440x900.png)
- Video build record: [../artifacts/design-qa/demo-video-results.json](../artifacts/design-qa/demo-video-results.json)
- Deterministic critic: `python -B scripts/weft_website_critic.py`
- Protocol and launch tests: `python -B -m unittest discover -s tests -v` -> 226 passing
- Rendered browser gate: `node scripts/capture-site-qa.cjs` -> exit 0
- Matching-runtime performance gate: re-baselined 2026-08-05 to ~68.8ms weighted median against the current extended harness; earlier 1,265.771ms -> 59.314ms (95.31%) was measured against a 3-scenario harness that no longer exists — see docs/PERFORMANCE.md provenance
- Native MiMo v2.5 production-web review: PASS, no P0/P1 local defect; domain-independent share metadata findings implemented

## External launch inputs

The repository does not identify a public domain or founder-owned contact/form endpoint. Therefore:

- absolute canonical, Open Graph, and sitemap URLs remain pending a domain;
- direct public lead delivery remains pending a contact destination;
- no placeholder domain, inbox, analytics ID, or fake submission backend was invented.

## Workflow provenance

The active Codex goal was used for the website mission. The installed `$autoresearch-goal` skill expects the `omx autoresearch-goal` CLI for durable mission and verdict bookkeeping, but `omx` is not available in the current shell and the previously installed global package is absent. This report and the deterministic critic are the project-local fallback evidence; they do not claim that a new OMX mission lifecycle was reconciled.

## Professor verdict

PASS when the fresh unit suite, rendered browser harness, `design-qa.md`, and `scripts/weft_website_critic.py` all pass against the same working tree. External domain and lead-delivery inputs remain an explicit go-live handoff, not a hidden implementation claim.
