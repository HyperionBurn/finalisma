# Finalisma launch-asset inventory

## Public website

- `site/index.html` — landing page and interactive reconciliation
- `site/demo.html` — recorded-proof player and transcript
- `site/docs/` — quickstart, protocol, security, and compatibility guides
- `site/blog/` — three launch field notes and index
- `site/llms.txt` — AI-readable product and boundary summary
- `site/site.webmanifest` — install/share metadata
- `site/404.html` — branded not-found page

## Social and video

- `site/assets/og-card.png` — 1200 x 630 social card
- `site/assets/finalisma-demo.mp4` — 6,772,088-byte H.264 launch video
- `site/assets/finalisma-demo.webm` — 3,459,479-byte VP8 browser recording
- `site/assets/finalisma-demo-poster.png` — 1280 x 720 video poster
- `site/assets/finalisma-demo.vtt` — English captions
- `site/assets/demo-transcript.json` — public redacted run record

## Launch copy

- `docs/PRODUCT_HUNT.md` — listing, first comment, replies, checklist
- `docs/YC_APPLICATION.md` — truthful application draft
- `docs/DEMO_VIDEO.md` — regeneration and claim boundary
- `docs/GO_LIVE.md` — release sequence and gates
- `docs/AUTORESEARCH_WEBSITE_LAUNCH_2026-07-30.md` — website research record
- `scripts/build-site-release.py` — injects the real HTTPS origin and founder
  contact, then emits an isolated sitemap-ready static release

## Verification evidence

- `design-qa.md` — human visual and interaction verdict
- `artifacts/design-qa/qa-results.json` — browser QA result
- `artifacts/design-qa/demo-video-results.json` — video build result
- `artifacts/design-qa/comparison-desktop-1440x900.png` — source/implementation comparison

The complete self-contained `site/` bundle is 10.16 MiB. The public player reads both video files as 43.04 seconds at 1280 x 720; the edited narrative is described as the 42-second proof.

## External values still required

- public canonical domain;
- absolute public Open Graph and video URLs;
- founder-owned contact or form endpoint;
- Product Hunt listing URL after creation;
- measured design-partner usage before replacing any traction placeholders.

No placeholder domain, inbox, customer logo, testimonial, analytics ID, or traction number is included.
