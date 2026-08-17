# Weft website design QA

## Scope and source

The source visual target is [site/design-target.svg](site/design-target.svg): the FIELD NOTES magazine system, with oxblood cover bands alternating with paper-stone article bands, Fraunces display serif with true italics, Big Shoulders Display labels, a 12-column grid, and ASSERT/PROVE colour roles. (The DOUBLE ENTRY ledger system is superseded and was deleted; see AGENT_HANDOVER.md §2.1.)

The implementation under review is [site/index.html](site/index.html), the [recorded-proof watch page](site/demo.html), plus the self-contained guides, field notes, 404 page, license page, and reconciliation interaction. The conversion goal is one design-partner application for a non-production incident handoff—not a generic waitlist.

The differentiating visual move is the reconciliation plane: on desktop it begins below the page surface, rises through the account as entries post, and settles flat only when the evidence account closes. DFII: 15 = impact 4 + fit 5 + feasibility 4 + performance 4 - consistency risk 2.

## Comparison history

| Pass | Source and implementation | Finding | Repair |
| --- | --- | --- | --- |
| 1 | 1440 x 900 target vs homepage | The product was visually distinctive but claimed a verified handoff without host evidence; mobile spent most of the first fold on two stacked top bars; the final story CTA was visible before balance. | Reframed the page around one incident account, published 9 documented / 0 verified, collapsed the mobile announcement, and made hidden state authoritative. |
| 2 | Desktop story at 0/25/50/75/100 | The 3D plane advanced correctly, but dense 28px inherited line height pushed the seventh row behind the footer. | Gave the body seven bounded grid rows, compact internal line heights, and an executable fit assertion. |
| 3 | 390 x 844, 390 x 667, 320 x 568 | Sticky choreography was brittle on short touch screens and obscured the core action. | Replaced mobile scroll choreography with a static tap-through ledger; the standard 390 x 844 and short 390 x 667 folds both include the primary CTA. |
| 4 | Guides and field-note index at 1440 x 900 | Field-note arrows wrapped into a second grid row. | Grouped each article title and summary so the arrow remains in the third column. |
| 5 | Recorded-proof page at 1440 x 900 | The first player layout pushed most of the proof below the fold, and metadata-only playback produced a harmless request-abort signal. | Rebuilt the first fold as a 37/63 narrative/player split and limited the exception to the two validated local media files. |
| Final | Side-by-side target and rendered homepage | Ledger grammar, split alignment, type hierarchy, square controls, evidence language, and first-fold purpose agree. No visible P0, P1, or P2 mismatch remains. | Passed. |

## Rendered artifacts

- [side-by-side target and implementation](artifacts/design-qa/comparison-desktop-1440x900.png)
- [desktop homepage](artifacts/design-qa/implementation-desktop-1440x900.png)
- [recorded-proof watch page](artifacts/design-qa/implementation-demo-1440x900.png)
- [desktop story 0%](artifacts/design-qa/implementation-story-0pct-1440x900.png)
- [desktop story 25%](artifacts/design-qa/implementation-story-25pct-1440x900.png)
- [desktop story 50%](artifacts/design-qa/implementation-story-50pct-1440x900.png)
- [desktop story 75%](artifacts/design-qa/implementation-story-75pct-1440x900.png)
- [desktop story 100%](artifacts/design-qa/implementation-story-100pct-1440x900.png)
- [mobile 390 x 844](artifacts/design-qa/implementation-mobile-390x844.png)
- [mobile short 390 x 667](artifacts/design-qa/implementation-mobile-short-390x667.png)
- [mobile narrow 320 x 568](artifacts/design-qa/implementation-mobile-narrow-320x568.png)
- [compatibility guide](artifacts/design-qa/implementation-guides-1440x900.png)
- [field-note index](artifacts/design-qa/implementation-blog-1440x900.png)
- [machine-readable QA result](artifacts/design-qa/qa-results.json)

Desktop density is 1440 x 900 at deviceScaleFactor 1. Mobile coverage is 390 x 844, 390 x 667, and 320 x 568 at deviceScaleFactor 1.

## Visual verdict

- The fixed rule lands at 532.797 CSS pixels on a 1440px viewport. Header, entries, totals, FAQ answers, and footer differ by at most 0.003px.
- The first fold explains the job, boundary, primary proof, cohort action, and 9/0 evidence status without a generic hero card or decorative asset.
- The watch-page first fold places the complete 16:9 proof player beside the factual run boundary; the poster exposes the consumed pairing, replay cursor, evidence result, and task result before playback.
- Red remains accounting state. All 11 unposted rows also contain a textual state marker.
- The 3D depth is limited to the live reconciliation plane. Supporting pages remain flat ledger records, preserving the system's hierarchy.
- Desktop rows stay inside the pinned body at all five scroll checkpoints. Mobile uses normal document flow and has no clipped sticky panel.
- Guides and field notes reuse the same type, split, rules, and interaction grammar instead of presenting a second design system.

## Interaction verdict

- Desktop scroll advances 0 -> 2 -> 4 -> 5 -> 7 posted rows at the five captured checkpoints, stays pinned at 64px, and settles the plane flat at balance.
- Manual controls independently reach `closed on evidence / balanced`, reveal the cohort CTA only at balance, and reset to `unopened / open`.
- Mobile tap-through reaches `balanced`; the panel is static, compact, fully visible, and flat at all three viewports.
- The mobile navigation is inert while closed, becomes interactive while open, traps keyboard focus, closes on Escape, and returns to inert.
- Copy feedback and application feedback use polite live regions.
- The design-partner form validates required fields and prepares a portable application through Web Share, clipboard, or a text download. It explicitly states that nothing is transmitted.
- The custom server returns the branded 404 body with an actual HTTP 404 status.

## Accessibility and resilience

- One H1 per page; labelled form controls; named links and buttons; skip links; visible focus treatment.
- No-JavaScript mode leaves every reveal and type treatment visible and exposes a scrollable mobile fallback navigation instead of a dead menu button.
- Reduced-motion mode removes sticky travel and 3D transform while keeping the full story readable and controllable.
- Forced-colours rules retain visible entry marks and textual state.
- The primary proof CTA is fully visible in the first fold at 390 x 844, 390 x 667, and 320 x 568.
- No horizontal overflow or overflow offenders were observed at any mobile viewport.

## Product truth and information architecture

- The homepage says exactly what transfers and what remains with each host.
- Compatibility is published as nine documented MCP paths and zero live Weft host validations. Codex + Claude Code is a proposed first proof pair, not a completed integration.
- “Coordination Protocol Preview 0.1” replaces “A2A Standard 1.0.”
- The site bundle now includes a local quickstart, protocol preview, security boundary, compatibility ledger, three field notes, MIT license, and branded 404. All internal static links resolve inside `site/`.
- The bundle also contains a reproducible MP4/WebM proof, poster, English captions, and AI-readable redacted run transcript. The coordinator path is real; the two agent hosts are explicitly deterministic fixtures.
- The offer is concrete: eight 10-100-person AI-native engineering teams, 30 days, a non-production incident mirror, a $500 deposit credited toward a $1,000/workspace/month pricing hypothesis.

## Fresh verification

`python -B -m unittest discover -s tests -v` passed the historical website-focused
65-test harness; the current local repository evidence is recorded in
[`docs/RELEASE_EVIDENCE.md`](docs/RELEASE_EVIDENCE.md) (1162 discovered, 1161
passed, 1 skipped). This does not claim browser or production proof.

`node scripts/capture-site-qa.cjs` exits 0 with:

- zero console errors;
- zero failed requests;
- zero HTTP error responses;
- zero third-party runtime requests;
- six homepage resources and 184,382 transferred bytes;
- local observed LCP 224ms and CLS 0.000026;
- no long task over 200ms;
- passing desktop, mobile, reduced-motion, no-JavaScript, guide, blog, and recorded-proof checks;
- validated 43.04-second, 1280 x 720 MP4 and WebM sources with a default English caption track and no horizontal overflow.

The local timings are regression signals, not public field-performance claims.

The locked matching-runtime coordinator evaluator's historical result was 1,265.771ms baseline to 59.314ms weighted median (95.31% improvement), with all three semantic digests unchanged, 65 tests and protocol smoke green, and no raw credential in output. The current reference artifact is 72.221ms; the latest complete local gate measured 137.404ms weighted median / 155.381ms p95 and failed its timing guards while the quality sub-gates passed. A controlled idle-host rerun remains required before attributing that regression to code. These are same-machine SQLite hot-path results, not model-speed or network claims.

A separate native MiMo v2.5 production-web review returned PASS with no P0 or P1 local defect. Its domain-independent share findings (`og:site_name` and explicit `twitter:image`) were implemented and test-locked. Absolute social/canonical URLs, `og:url`, and a standards-valid sitemap remain correctly deferred until a real deployment origin exists.

## Remaining external launch inputs

These are not local design defects and were not fabricated:

- A public domain is required before absolute canonical, Open Graph, and sitemap URLs can be finalized.
- A founder-owned contact or form endpoint is required if public Product Hunt visitors should submit directly. The current application builder is real and privacy-preserving, but it is intentionally not a fake lead backend.

final result: passed
