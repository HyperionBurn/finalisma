# Weft website completion audit

Date: 2026-07-31  
Objective: `go all in on the website`

## Verdict

The project-local website release candidate is complete and verified. A public
launch is not yet complete because the repository intentionally contains neither
a real HTTPS origin nor a founder-owned contact destination. Those values cannot
be inferred or fabricated.

## Requirement evidence

| Requirement | Authoritative evidence | Status |
| --- | --- | --- |
| Distinctive production-quality landing page | `site/index.html`, `site/styles.css`, `artifacts/design-qa/implementation-desktop-1440x900.png`, `design-qa.md` | Proven locally |
| Clear first-time buyer positioning | First fold states one incident, two agents, exact transfer boundary, 9 documented paths, and 0 validated host integrations | Proven locally |
| Desktop freeze-scroll 3D product story | `site/app.js`, reconciliation styles, five captured checkpoints, pinned-offset and depth assertions in `qa-results.json` | Proven locally |
| Mobile experience | 390x844, 390x667, and 320x568 captures; proof CTA visible before scroll; tap-through reaches balanced | Proven locally |
| Reduced-motion and no-JavaScript resilience | Browser harness families `reducedMotionChecks`, `noJsChecks`, and `noJsMobileChecks` | Proven locally |
| Recorded product proof | 43.04-second 1280x720 MP4/WebM, poster, VTT captions, redacted transcript, and watch page | Proven locally |
| Truthful proof boundary | Real coordinator/storage/pairing/replay/lease/evidence path; deterministic host fixtures labelled in every public proof surface | Proven locally |
| Guides, articles, license, AI readability | Self-contained `site/docs/`, `site/blog/`, `site/license.html`, `site/llms.txt`, SoftwareApplication and VideoObject metadata | Proven locally |
| YC and Product Hunt launch package | `docs/YC_APPLICATION.md`, `docs/PRODUCT_HUNT.md`, `docs/DEMO_VIDEO.md`, `docs/LAUNCH_ASSETS.md`, `docs/GO_LIVE.md` | Proven locally; founder facts still require filling |
| Conversion without a fake backend | Local application builder validates and exports through share, clipboard, or download; explicitly transmits nothing | Proven locally |
| Public-domain metadata and direct founder contact | `scripts/build-site-release.py` materializes absolute metadata, contact CTA, sitemap, robots declaration, and media manifest from supplied values | Implementation proven; real values missing |
| Static/runtime correctness | 868 tests pass; deterministic website critic passes; browser QA has zero console errors, failed requests, or bad responses | Proven locally |
| Performance regression gate | Current locked baseline is ~72.2ms weighted median; latest strict run measured ~62.3ms with matching semantics and all p95, smoke, credential, and 868-test gates passing. See docs/PERFORMANCE.md. | Proven locally |
| Independent production-web review | Native MiMo v2.5 review returned PASS with no local P0/P1 defect; its domain-independent share findings were implemented | Proven locally |

## Deliberate implementation decision

The requested freeze-scroll and 3D behavior is implemented with native sticky
layout, passive scroll handling, `requestAnimationFrame`, CSS variables, and
scroll-driven CSS instead of shipping GSAP or Lenis. The behavior is covered by
rendered checkpoints and reduced-motion fallbacks. This is a documented
implementation substitution, not a claim that GSAP is installed.

## Exact external inputs still required

1. Public HTTPS origin, such as `https://weft.example`.
2. Founder-owned HTTPS application endpoint or `mailto:` destination.

Once both are real, run:

```powershell
python -B .\scripts\build-site-release.py `
  --origin https://THE-REAL-ORIGIN `
  --contact-url mailto:THE-REAL-FOUNDER-ADDRESS
```

The generated `artifacts/release-site/` is the upload target. Public hosting,
DNS, social-card validation against the deployed origin, and marketplace
submission cannot be evidenced before those external values exist.

## Hygiene

- No global package, PATH, registry, shell-profile, service, or startup change.
- No project `node_modules`, Python cache, failed-release output, or `.tmp` residue.
- The local source remains free of placeholder domains and contact addresses.
