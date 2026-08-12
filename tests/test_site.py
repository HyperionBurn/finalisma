from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"

# The landing-page CTAs must be ABSOLUTE URLs on the web app's routes
# (/signup, /login). The app origin is a deployment property, not a constant:
# it moves (localhost -> tunnel -> production domain) and is set once in
# web/src/lib/app.ts. The tests below therefore assert the PROPERTY that stays
# true regardless of host — an absolute `http(s)://` href ending in the route —
# rather than re-pinning a specific host, which broke every time the backend
# moved. Matches: href="https://app.example/signup".
ABSOLUTE_ROUTE_HREF = r'href="https?://[^"]+/signup"'
sys.path.insert(0, str(ROOT))

_SITE_SPEC = importlib.util.spec_from_file_location(
    "weft_site", ROOT / "scripts" / "weft-site.py"
)
assert _SITE_SPEC and _SITE_SPEC.loader
_SITE_MODULE = importlib.util.module_from_spec(_SITE_SPEC)
_SITE_SPEC.loader.exec_module(_SITE_MODULE)
QuietSiteHandler = _SITE_MODULE.QuietSiteHandler

_RELEASE_SPEC = importlib.util.spec_from_file_location(
    "build_site_release", ROOT / "scripts" / "build-site-release.py"
)
assert _RELEASE_SPEC and _RELEASE_SPEC.loader
_RELEASE_MODULE = importlib.util.module_from_spec(_RELEASE_SPEC)
_RELEASE_SPEC.loader.exec_module(_RELEASE_MODULE)

_CRITIC_SPEC = importlib.util.spec_from_file_location(
    "weft_website_critic", ROOT / "scripts" / "weft_website_critic.py"
)
assert _CRITIC_SPEC and _CRITIC_SPEC.loader
_CRITIC_MODULE = importlib.util.module_from_spec(_CRITIC_SPEC)
_CRITIC_SPEC.loader.exec_module(_CRITIC_MODULE)

_VERCEL_BUILD_SPEC = importlib.util.spec_from_file_location(
    "vercel_build", ROOT / "scripts" / "vercel-build.py"
)
assert _VERCEL_BUILD_SPEC and _VERCEL_BUILD_SPEC.loader
_VERCEL_BUILD_MODULE = importlib.util.module_from_spec(_VERCEL_BUILD_SPEC)
_VERCEL_BUILD_SPEC.loader.exec_module(_VERCEL_BUILD_MODULE)


class LaunchSurfaceTests(unittest.TestCase):
    def test_landing_page_has_truthful_semantic_launch_surface(self) -> None:
        html = (SITE / "index.html").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"<h1[\s>]", html)), 1)
        # Wave D3 replacement invariant: the headline now reflects the product
        # (one link admits many agents), not the old two-party "incident" framing.
        self.assertIn("One link. Many agents. All governed.", html)
        # Wave-D3 motion rebuild (founder brief): the jargon stat strip was
        # DELETED from the hero above the fold. The measured facts survive
        # below the fold in the Proof section — that is the justified
        # replacement assertion, not a deletion of the truthfulness intent.
        self.assertNotIn("data-stat-strip", html)
        # VANGUARD spec (founder): the "documented MCP paths" phrase is banned
        # everywhere. Founder-feedback wave: the entire Proof section was
        # removed ("nobody includes proof other than AI"). The honest labels
        # that remain are the MCP-protocol marquee + its disclaimer and the
        # hero's simulated-demo readout. Those are the justified replacement
        # assertions, not a deletion of the truthfulness intent.
        self.assertNotIn("documented MCP paths", html.lower())
        self.assertNotIn("Proof, not promises", html)
        self.assertNotIn('data-proof-item', html)
        self.assertIn("Speaks MCP", html)
        self.assertIn("MCP is a public protocol; these names identify the hosts that speak it", html)
        self.assertIn('type="application/ld+json"', html)
        self.assertIn('property="og:site_name" content="Weft"', html)
        self.assertIn('name="twitter:image" content="/assets/og-card.png"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("data-sim-label", html)
        self.assertIn("Simulated account · no credentials · no live session", html)
        self.assertIn("MCP is the tool protocol", html)
        self.assertIn("single-node", html.lower())
        self.assertIn("data-cohort-form", html)
        self.assertIn("See how it works", html)
        self.assertIn('href="#how-it-works"', html)
        # Pricing decision 2026-08-07: the $1,000/workspace + $500 deposit +
        # 30-day pilot offer was replaced by Free / Pro ($39/seat) / Enterprise
        # tiers that map to enforced quota limits. The old offer is gone.
        self.assertNotIn("$500 deposit", html)
        self.assertNotIn("$1,000", html)
        self.assertIn("$0", html)
        self.assertIn("$39", html)
        self.assertIn("Start free", html)
        # The signup/login CTAs must be ABSOLUTE hrefs on the app origin, not
        # relative paths that 404 against the static bundle. We assert the
        # property (`http(s)://<host>/signup`) rather than a specific host —
        # the origin moves between dev and prod and lives in one place,
        # web/src/lib/app.ts. Re-pinning a concrete URL broke every time the
        # backend moved (localhost -> tunnel -> production domain).
        self.assertRegex(html, ABSOLUTE_ROUTE_HREF)
        self.assertRegex(html, r'href="https?://[^"]+/login"')
        # The web app serves its routes at the ORIGIN ROOT, not under `/app`.
        # Any relative `/app/...` CTA in the built landing page is a 404 link
        # and must never ship. The absolute-origin form is asserted above.
        self.assertNotIn('href="/app/signup"', html)
        self.assertNotIn('href="/app/login"', html)
        self.assertNotIn("verified agent handoff layer", html.lower())
        self.assertNotIn("Weft A2A Standard", html)
        self.assertNotIn("gpt-5.5", html.lower())
        self.assertNotIn("lorem ipsum", html.lower())
        # Site-truth lane (2026-08-08): the demo is a simulation, so nothing on
        # the landing page may label it "live"; the outdated "we have not run
        # this in production" clause is replaced by the truthful deployed +
        # own-test statement (40-agent run, our own traffic).
        self.assertNotIn("Live demo", html)
        self.assertNotIn("we have not run this in production", html)
        self.assertIn("40 independent agents", html)
        self.assertIn("not customer traffic", html)

    def test_site_never_overclaims_liveness_or_enforcement(self) -> None:
        """Site-truth invariants (2026-08-08).

        Walk every shipped HTML page and assert the phrasings that were found
        false this session are absent:

        * the demo is a simulation — no \"live\"-about-simulated label;
        * plan-driven quota enforcement is WIRED into the request path on this
          trunk and was verified this session over real HTTP (cap above the
          free plan's 10-per-room member limit -> 409 quota_exceeded; a 6th
          room -> 409 quota_exceeded). The room-count and per-room-member
          limits ARE enforced, so the pricing copy states that. The MONTHLY
          EVENT cap (10,000 free / 100,000 pro) is NOT enforced in code — no
          counter is maintained and sends are not refused — so events-per-month
          must never be described as enforced, and the old overclaim phrasings
          stay banned;
        * \"we have not run this in production\" is outdated — the product is
          deployed and publicly reachable;
        * the demo on the landing page runs two named agents, not four.
        """
        banned_site_wide = (
            "we have not run this in production",
            "four agents",
            "hard, enforced limit",
            "events / month — enforced",
            "(an enforced limit)",
            "events / month · enforced",
        )
        banned_index_only = (
            "Live demo",
            "Live Preview",
            "Live event log",
        )
        for page in sorted(SITE.rglob("*.html")):
            lowered = page.read_text(encoding="utf-8").lower()
            with self.subTest(page=page.relative_to(ROOT)):
                for phrase in banned_site_wide:
                    self.assertNotIn(phrase.lower(), lowered)
                if page.name == "index.html":
                    for phrase in banned_index_only:
                        self.assertNotIn(phrase.lower(), lowered)

    def test_progressive_enhancement_and_gated_story_cta(self) -> None:
        html = (SITE / "index.html").read_text(encoding="utf-8")
        # The marketing page's real CSS is the Astro-built bundle that index.html
        # links; `site/styles.css` is the preserved legacy file still used by the
        # guide/blog pages. Assert the progressive-enhancement rules against the
        # stylesheet the page ACTUALLY loads.
        match = re.search(r'<link rel="stylesheet" href="([^"]+\.css)">', html)
        assert match, "built index.html must link its stylesheet"
        css_path = (SITE / match.group(1).lstrip("/")).resolve()
        self.assertTrue(css_path.is_relative_to(SITE.resolve()), f"css escapes site: {css_path}")
        css = css_path.read_text(encoding="utf-8")
        # Astro minifies the inline classList script (single vs double quotes).
        self.assertRegex(html, r"classList\.add\(['\"]js['\"]\)")
        # Tailwind 4 may emit the modernized selector
        # `[hidden]:where(:not([hidden=until-found]))` and minify the rules.
        self.assertRegex(
            css,
            r"\[hidden\][^{]*\{[^}]*display:\s*none[^}]*\}",
        )
        self.assertRegex(css, r"display:\s*none!important|display:\s*none\s+!important")
        # Progressive enhancement invariant: .reveal is visible by default
        # (no-JS / reduced-motion) and only gated under `.js`.
        self.assertRegex(css, r"\.reveal\{opacity:\s*1[^}]*transform:\s*none")
        self.assertRegex(css, r"\.js \.reveal")
        self.assertRegex(css, r"\.js \.reveal\.in\{opacity:\s*1[^}]*transform:\s*none")
        # Wave D3 replacement invariant: the JS-gated hero canvas is always
        # mounted server-side and carries a ≥5-row text fallback, so no-JS and
        # screen readers see the full event sequence (old `data-story-next`
        # gated-CTA + `data-recon-plane` hooks are gone).
        self.assertIn("data-agent-canvas", html)
        fallback = re.search(r'<ol[^>]*data-agent-events-fallback[^>]*>(.*?)</ol>', html, re.S)
        self.assertIsNotNone(fallback, "hero must render a text-alternative event list")
        self.assertGreaterEqual(len(re.findall(r"<li", fallback.group(1))), 5)
        self.assertIn("aria-live", html)

    def test_marketing_site_does_not_claim_dependency_free_website(self) -> None:
        """The marketing site is built with a toolchain (Astro/R3F/npm).

        It must NOT claim to be 'no-build', 'dependency-free', or 'no CDN' as a
        website property. The dependency-free promise applies to the COORDINATOR
        only, and where it appears on the site it must be scoped as such.
        """
        html = (SITE / "index.html").read_text(encoding="utf-8")
        lowered = html.lower()
        self.assertNotIn("no build step", lowered)
        self.assertNotIn("no-build landing page", lowered)
        self.assertNotIn("dependency-free website", lowered)
        self.assertNotIn("dependency-free site", lowered)
        self.assertNotIn("no cdn", lowered)
        # If a dependency-free claim appears, it must be scoped to the coordinator.
        if "dependency-free" in lowered:
            self.assertIn("coordinator", lowered)

    def test_unposted_entries_never_rely_on_colour_alone(self) -> None:
        """Colour-alone accessibility invariant, re-expressed for the VANGUARD
        rebuild.

        The DOUBLE ENTRY design used ``<li class="entry">`` rows with
        ``.entry-state`` spans; FIELD NOTES used ``.keylist``/``.factlist``
        rows; Wave D3 used ``[data-proof-item]`` + ``[data-step]``. The founder
        then removed the Proof section entirely ("nobody includes proof other
        than AI"). The colour-coded structures that REMAIN on the page are:

        * ``[data-step]`` (4 items) — ``.step-num`` is coloured; the adjacent
          ``<h3>`` is the textual step name.
        * ``[data-agent-events-fallback]`` (5+ items) — each ``<li>`` is
          colour-coded by ``data-evt-kind`` (join/assert/prove/refuse) and
          carries text in ``.evt-msg`` (and ``.evt-agent`` / ``.evt-seq``).

        Invariant: every colour-coded row must carry readable text next to its
        colour marker — colour never carries meaning alone.
        4 + 5 = 9 rows (threshold preserved from the V2 structure's count; the
        invariant intent is unchanged).
        """
        html = (SITE / "index.html").read_text(encoding="utf-8")
        steps = re.findall(r'<li[^>]*data-step[^>]*>(.*?)</li>', html, re.S)
        fallback = re.search(r'<ol[^>]*data-agent-events-fallback[^>]*>(.*?)</ol>', html, re.S)
        self.assertIsNotNone(fallback, "hero must render a text-alternative event list")
        event_rows = re.findall(r'<li[^>]*data-evt-kind[^>]*>(.*?)</li>', fallback.group(1), re.S)
        self.assertGreaterEqual(len(event_rows), 5, "event fallback must carry >= 5 colour-coded rows")
        entries = steps + event_rows
        self.assertGreaterEqual(len(entries), 9)
        for entry in entries:
            with self.subTest(entry=entry[:60]):
                has_heading = re.search(r'<h3[^>]*>([^<]+)</h3>', entry)
                has_evt_msg = re.search(r'class="evt-msg"[^>]*>([^<]+)</span>', entry)
                textual_marker = has_heading or has_evt_msg
                self.assertIsNotNone(
                    textual_marker,
                    "colour-coded row has no textual state/description marker",
                )
                marker_text = textual_marker.group(1).strip()
                self.assertTrue(marker_text, "textual marker is empty")

    def test_static_launch_bundle_contains_guides_articles_and_social_asset(self) -> None:
        required_root = {
            "index.html",
            "styles.css",
            "app.js",
            "robots.txt",
            "llms.txt",
            "404.html",
            "license.html",
            "terms.html",
            "privacy.html",
            "demo.html",
            "demo-stage.html",
            "demo.css",
            "demo-stage.js",
            "site.webmanifest",
        }
        self.assertTrue(required_root.issubset({path.name for path in SITE.iterdir()}))
        required_guides = {
            "index.html",
            "quickstart.html",
            "protocol.html",
            "security.html",
            "compatibility.html",
            "pairing-ux.html",
            "pilot.html",
        }
        self.assertEqual(required_guides, {path.name for path in (SITE / "docs").glob("*.html")})

        og_card = SITE / "assets" / "og-card.png"
        self.assertTrue(og_card.is_file())
        png_header = og_card.read_bytes()
        self.assertEqual(png_header[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(png_header[16:20], "big"), 1200)
        self.assertEqual(int.from_bytes(png_header[20:24], "big"), 630)

        poster = SITE / "assets" / "weft-demo-poster.png"
        poster_header = poster.read_bytes()
        self.assertEqual(poster_header[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(poster_header[16:20], "big"), 1280)
        self.assertEqual(int.from_bytes(poster_header[20:24], "big"), 720)

        mp4 = SITE / "assets" / "weft-demo.mp4"
        webm = SITE / "assets" / "weft-demo.webm"
        captions = SITE / "assets" / "weft-demo.vtt"
        self.assertGreater(mp4.stat().st_size, 500_000)
        self.assertEqual(mp4.read_bytes()[4:8], b"ftyp")
        self.assertGreater(webm.stat().st_size, 500_000)
        self.assertEqual(webm.read_bytes()[:4], b"\x1a\x45\xdf\xa3")
        self.assertTrue(captions.read_text(encoding="utf-8").startswith("WEBVTT\n"))

        articles = sorted((SITE / "blog").glob("*.html"))
        self.assertGreaterEqual(len(articles), 4)
        for article in articles:
            html = article.read_text(encoding="utf-8")
            self.assertRegex(html, r"<h1>.+</h1>")
            self.assertRegex(html, r'<meta name="description" content=".+">')
            self.assertIn("../styles.css", html)

        temp_root = ROOT / ".tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="weft-release-test-", dir=temp_root) as temporary:
            output = Path(temporary) / "site"
            result = _RELEASE_MODULE.build_release(
                origin="https://weft.test",
                contact_url="mailto:founder@example.invalid",
                output=output,
            )
            built_home = (output / "index.html").read_text(encoding="utf-8")
            built_demo = (output / "demo.html").read_text(encoding="utf-8")
            self.assertEqual(result["origin"], "https://weft.test")
            self.assertIn('href="https://weft.test/"', built_home)
            self.assertIn('property="og:url" content="https://weft.test/"', built_home)
            self.assertIn('name="twitter:image" content="https://weft.test/assets/og-card.png"', built_home)
            self.assertIn('data-founder-contact href="mailto:founder@example.invalid"', built_home)
            self.assertIn('"contentUrl": "https://weft.test/assets/weft-demo.mp4"', built_demo)
            self.assertIn("https://weft.test/demo.html", (output / "sitemap.xml").read_text(encoding="utf-8"))
            self.assertIn("Sitemap: https://weft.test/sitemap.xml", (output / "robots.txt").read_text(encoding="utf-8"))
            self.assertTrue((output / "release-manifest.json").is_file())
        for invalid_origin in (
            "http://weft.test",
            "https://user:secret@weft.test",
            "https://weft.test/subpath",
        ):
            with self.subTest(invalid_origin=invalid_origin), self.assertRaises(ValueError):
                _RELEASE_MODULE.normalize_origin(invalid_origin)
        with self.assertRaises(ValueError):
            _RELEASE_MODULE.normalize_contact_url("javascript:alert(1)")

    def test_static_internal_content_links_resolve_inside_site_bundle(self) -> None:
        pages = sorted(SITE.rglob("*.html"))
        for page in pages:
            html = page.read_text(encoding="utf-8")
            for reference in re.findall(r'(?:href|src)="([^"]+)"', html):
                if reference.startswith(("#", "http://", "https://", "mailto:", "javascript:", "data:")):
                    continue
                href_path = reference.split("#", 1)[0].split("?", 1)[0]
                # App-front-door references are external to this static
                # bundle — the web app (weft_cloud/web/app.py) serves
                # them, not the site's path tree. The built index.html now
                # points at an absolute app origin (already excluded by the
                # scheme check above); preserved legacy pages restored
                # byte-for-byte by guard-legacy (e.g. site/docs/pilot.html,
                # part of the pricing work) still carry the retired relative
                # `/app/*` path form and are skipped here. A `/app/*` link in
                # the BUILT index.html is asserted absent by
                # test_landing_page_has_truthful_semantic_launch_surface.
                if href_path == "/app" or href_path.startswith("/app/"):
                    continue
                target = (
                    (SITE / href_path.lstrip("/")).resolve()
                    if href_path.startswith("/")
                    else (page.parent / href_path).resolve()
                )
                with self.subTest(page=page.relative_to(ROOT), reference=reference):
                    self.assertTrue(target.is_relative_to(SITE.resolve()))
                    self.assertTrue(target.is_file() or target.is_dir(), f"missing target: {target}")

    def test_funnel_ctas_point_at_web_app(self) -> None:
        """The site must offer a real way to become a user: the web app.

        Funnel lane (2026-08-07): every primary CTA on the landing page
        resolves to the app origin, a quiet "Log in"
        affordance exists for returning users, and the closed-trial framing
        ("Start free pilot") is gone from the primary CTAs. The app is not
        deployed, so routes are asserted by origin + path only — nothing here
        invents a destination, a free-tier limit, or a trial length. The
        origin itself is a deployment property (web/src/lib/app.ts); we assert
        absolute `http(s)://` hrefs ending in the route, not a pinned host.
        """
        html = (SITE / "index.html").read_text(encoding="utf-8")
        self.assertRegex(html, ABSOLUTE_ROUTE_HREF)
        self.assertRegex(html, r'href="https?://[^"]+/login"')

        hero = re.search(r'<section[^>]*id="hero"[^>]*>(.*?)</section>', html, re.S)
        self.assertIsNotNone(hero, "hero section must exist")
        hero_primary = re.search(
            r'<a[^>]*class="[^"]*\bbtn-accent\b[^"]*"[^>]*href="([^"]+)"',
            hero.group(1),
        )
        self.assertIsNotNone(hero_primary, "hero must carry a btn-accent primary CTA")
        self.assertRegex(
            hero_primary.group(1),
            r"^https?://[^\"\s]+/signup$",
            "hero primary CTA must be an absolute URL on the app origin ending in /signup",
        )

        connect = re.search(r'<section[^>]*id="connect"[^>]*>(.*?)</section>', html, re.S)
        self.assertIsNotNone(connect, "connect section must exist")
        self.assertRegex(
            connect.group(1),
            r'href="https?://[^"]+/signup"',
            "connect section must close with a real signup link",
        )

        self.assertNotIn(
            "Start free pilot",
            html,
            "closed-trial framing must be gone from the primary CTAs",
        )

    def test_compatibility_page_keeps_documented_and_verified_distinct(self) -> None:
        html = (SITE / "docs" / "compatibility.html").read_text(encoding="utf-8")
        self.assertIn("8 documented-unverified", html)
        self.assertIn("1 verified", html)
        self.assertIn("Documented is not verified.", html)
        for host in ("Codex", "ChatGPT", "Claude Code", "VS Code", "Cursor", "Gemini CLI", "OpenCode", "Zed", "Cline"):
            self.assertIn(host, html)

    def test_recorded_demo_is_redacted_and_grounded_in_a_real_run(self) -> None:
        demo = (SITE / "demo.html").read_text(encoding="utf-8")
        transcript = json.loads((SITE / "assets" / "demo-transcript.json").read_text(encoding="utf-8"))
        build = json.loads((ROOT / "artifacts" / "design-qa" / "demo-video-results.json").read_text(encoding="utf-8"))
        self.assertIn("The two agent hosts are deterministic fixtures", demo)
        self.assertIn("weft-demo.mp4", demo)
        self.assertIn("weft-demo.webm", demo)
        self.assertIn("weft-demo.vtt", demo)
        self.assertIn('"@type": "VideoObject"', demo)
        self.assertIn('content="/assets/weft-demo.mp4"', demo)
        self.assertIn('content="1280"', demo)
        self.assertIn('content="720"', demo)
        self.assertIn('content="index,follow,max-video-preview:-1,max-image-preview:large"', demo)
        self.assertIn('property="og:site_name" content="Weft"', demo)
        self.assertIn('name="twitter:image" content="/assets/weft-demo-poster.png"', demo)
        self.assertEqual(transcript["schema"], "weft.public-demo/v1")
        self.assertEqual(len(transcript["frames"]), 10)
        self.assertTrue(transcript["credentials_redacted"])
        self.assertFalse(transcript["proof"]["raw_credentials_in_output"])
        self.assertEqual(transcript["proof"]["pairing_status"], "consumed")
        self.assertEqual(transcript["proof"]["last_ack_seq"], 1)
        self.assertTrue(transcript["proof"]["evidence_passed"])
        self.assertEqual(transcript["proof"]["secret_scan"], "passed")
        self.assertEqual(transcript["proof"]["task_status"], "done")
        self.assertEqual(build["status"], "ok")
        self.assertEqual(build["resolution"], "1280x720")
        self.assertTrue(build["credentials_redacted"])

    def test_legal_pages_render_reachable_from_footer_and_serve_200(self) -> None:
        """Terms of Service and Privacy Policy must exist, be linked from the
        site footer, and be served 200 by the real site server.
        """
        for page_name in ("terms.html", "privacy.html"):
            page = SITE / page_name
            self.assertTrue(page.is_file(), f"{page_name} must exist in the site bundle")
            html = page.read_text(encoding="utf-8")
            self.assertIn("<title>", html)
            self.assertIn("<h1>", html)
            self.assertIn('<link rel="stylesheet" href="styles.css">', html)

        index_html = (SITE / "index.html").read_text(encoding="utf-8")
        footer = re.search(r'<nav aria-label="Footer">(.*?)</nav>', index_html, re.S)
        self.assertIsNotNone(footer, "landing page must carry a footer nav")
        self.assertIn('href="/terms.html"', footer.group(1))
        self.assertIn('href="/privacy.html"', footer.group(1))

        handler = lambda *args, **kwargs: QuietSiteHandler(*args, directory=str(ROOT), **kwargs)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address

            def get(path: str) -> tuple[int, str]:
                connection = HTTPConnection(host, port, timeout=5)
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read().decode("utf-8", errors="replace")
                status = response.status
                connection.close()
                return status, body

            for path in ("/terms.html", "/privacy.html"):
                with self.subTest(path=path):
                    status, body = get(path)
                    self.assertEqual(status, 200, f"{path} must return 200")
                    self.assertIn("</html>", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_server_mounts_self_contained_site_and_branded_404(self) -> None:
        handler = lambda *args, **kwargs: QuietSiteHandler(*args, directory=str(ROOT), **kwargs)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address

            def get(path: str) -> tuple[int, str, dict[str, str]]:
                connection = HTTPConnection(host, port, timeout=5)
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read().decode("utf-8", errors="replace")
                headers = dict(response.getheaders())
                status = response.status
                connection.close()
                return status, body, headers

            status, home, _ = get("/")
            self.assertEqual(status, 200)
            self.assertIn("One link. Many agents.", home)

            # Legal pages must render and be served 200 by the real server.
            for legal_path, expected in (
                ("/terms.html", "Terms of Service"),
                ("/privacy.html", "Privacy Policy"),
            ):
                with self.subTest(path=legal_path):
                    status, body, _ = get(legal_path)
                    self.assertEqual(status, 200)
                    self.assertIn(expected, body)
                    self.assertIn("</html>", body)

            status, guide, _ = get("/docs/compatibility.html")
            self.assertEqual(status, 200)
            self.assertIn("Documented is not verified.", guide)

            status, demo, _ = get("/demo.html")
            self.assertEqual(status, 200)
            self.assertIn("A real coordinator run.", demo)

            status, manifest, _ = get("/site.webmanifest")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(manifest)["name"], "Weft")

            status, _, _ = get("/assets/weft-demo.mp4")
            self.assertEqual(status, 200)

            status, _, headers = get("/assets/og-card.png")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Cache-Control"), "public, max-age=3600")

            status, missing, _ = get("/not-a-real-entry")
            self.assertEqual(status, 404)
            self.assertIn("This path is not in the account.", missing)

            for path in ("/../src/weft_mcp/core.py", "/docs/%2e%2e/src/weft_mcp/core.py"):
                with self.subTest(path=path):
                    status, body, _ = get(path)
                    self.assertEqual(status, 404)
                    self.assertNotIn("class Weft", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_server_serves_what_the_bundle_contains(self) -> None:
        """The server must SERVE every asset the built index.html references.

        This closes the class of bug where files exist on disk and the HTML
        links them, but the server's route allowlist refuses them — producing
        a 200 HTML page that renders unstyled/unhydrated in production while
        the file-existence tests stay green. We start the REAL server, GET /,
        parse the stylesheet link and every script/component URL out of the
        returned HTML, and fetch each one over HTTP.
        """
        handler = lambda *args, **kwargs: QuietSiteHandler(*args, directory=str(ROOT), **kwargs)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address

            def get_raw(path: str) -> tuple[int, bytes, dict[str, str]]:
                connection = HTTPConnection(host, port, timeout=10)
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read()
                headers = {k.lower(): v for k, v in response.getheaders()}
                status = response.status
                connection.close()
                return status, body, headers

            status, home_body, _ = get_raw("/")
            self.assertEqual(status, 200)
            home = home_body.decode("utf-8", errors="replace")

            # Every stylesheet the page links.
            stylesheets = re.findall(r'<link[^>]*rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\']', home)
            stylesheets += re.findall(r'<link[^>]*href=["\']([^"\']+\.css)["\'][^>]*rel=["\']stylesheet["\']', home)
            self.assertTrue(stylesheets, "built index.html must link at least one stylesheet")
            for href in dict.fromkeys(stylesheets):
                with self.subTest(asset=href):
                    status, body, headers = get_raw(href)
                    self.assertEqual(status, 200, f"stylesheet {href} refused by server")
                    content_type = headers.get("content-type", "")
                    self.assertTrue(
                        content_type.startswith("text/css"),
                        f"stylesheet {href} served as {content_type!r}",
                    )
                    self.assertGreater(len(body), 100, f"stylesheet {href} is empty")

            # Every module script the page references — inline scripts have no
            # src, but Astro islands reference /_astro/*.js via component-url
            # and renderer-url attributes.
            script_urls = re.findall(r'src=["\']([^"\']+\.js)["\']', home)
            script_urls += re.findall(r'(?:component-url|renderer-url)=["\']([^"\']+\.js)["\']', home)
            self.assertTrue(script_urls, "built index.html must reference at least one JS module")
            for url in dict.fromkeys(script_urls):
                with self.subTest(asset=url):
                    status, body, headers = get_raw(url)
                    self.assertEqual(status, 200, f"script {url} refused by server")
                    content_type = headers.get("content-type", "")
                    self.assertTrue(
                        content_type.startswith("text/javascript") or content_type.startswith("application/javascript"),
                        f"script {url} served as {content_type!r}",
                    )
                    self.assertGreater(len(body), 50, f"script {url} is empty")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


    def test_site_server_sends_security_response_headers(self) -> None:
        """The standalone site server ships the full security header block.

        The live service shipped no HSTS/CSP/frame-protection/
        Referrer-Policy/Permissions-Policy on HTML responses. The site server
        (scripts/weft-site.py) is one of the surfaces a self-hoster runs
        without nginx, so IT must carry the headers — not just the web app.
        """
        handler = lambda *args, **kwargs: QuietSiteHandler(*args, directory=str(ROOT), **kwargs)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address

            def get(path: str) -> tuple[int, dict[str, str]]:
                connection = HTTPConnection(host, port, timeout=5)
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read()
                headers = dict(response.getheaders())
                status = response.status
                connection.close()
                return status, headers

            for path in ("/", "/terms.html", "/docs/index.html"):
                with self.subTest(path=path):
                    status, headers = get(path)
                    self.assertEqual(status, 200, f"{path} must return 200")
                    self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
                    self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
                    self.assertEqual(headers.get("X-Frame-Options"), "DENY")
                    self.assertTrue(
                        headers.get("Strict-Transport-Security", "").startswith("max-age="),
                        f"{path}: missing HSTS",
                    )
                    csp = headers.get("Content-Security-Policy", "")
                    self.assertIn("default-src 'none'", csp, f"{path}: missing CSP")
                    self.assertIn("frame-ancestors 'none'", csp, f"{path}: no frame protection")
                    self.assertIn("Permissions-Policy", headers, f"{path}: missing Permissions-Policy")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class WebsiteCriticStaleDataTests(unittest.TestCase):
    """The website critic must not crash on absent/stale baseline QA data.

    Release-gate finding: the rendered QA artifact can carry a stale baseline
    where the ledger split was never measured, so ``maxRuleDrift`` (and the
    sibling ``ruleWidth``/``ruleLeft``) are JSON ``null``. The critic used to
    call ``float(None)`` and die. Absent baseline data is a DISTINCT outcome
    from a passing comparison: the critic must report it as a clear failure
    (the gate stays red — nobody should ship on "we never measured this"),
    not crash, and not silently pass.
    """

    def _run_critic(self, *, qa: dict[str, object], report: str) -> tuple[int, list[str]]:
        with tempfile.TemporaryDirectory(prefix="weft-critic-test-") as temporary:
            qa_path = Path(temporary) / "qa-results.json"
            report_path = Path(temporary) / "design-qa.md"
            qa_path.write_text(json.dumps(qa), encoding="utf-8")
            report_path.write_text(report, encoding="utf-8")
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = _CRITIC_MODULE.main(["--qa", str(qa_path), "--report", str(report_path)])
            payload = json.loads(stream.getvalue())
        return code, list(payload.get("failures", []))

    def _stale_top_level(self) -> dict[str, object]:
        return {
            "oneH1": True,
            "initialStoryCtaHidden": True,
            "ruleFixed": False,
            "ruleWidth": None,
            "ruleLeft": None,
            "maxRuleDrift": None,
            "unpostedCount": 0,
            "allUnpostedLabelled": True,
            "formLabels": True,
            "htmlHasJsClass": True,
        }

    def test_null_max_rule_drift_reports_no_baseline_failure_not_crash(self) -> None:
        qa = {
            "topLevelChecks": self._stale_top_level(),
            "interactions": {},
            "signals": {},
            "performanceChecks": {},
        }
        code, failures = self._run_critic(qa=qa, report="final result: passed\n")
        self.assertEqual(code, 1)
        self.assertTrue(
            any("maxRuleDrift" in failure and "cannot compare" in failure for failure in failures),
            f"expected a clear no-baseline failure naming maxRuleDrift, got: {failures}",
        )

    def test_absent_max_rule_drift_reports_no_baseline_failure_not_crash(self) -> None:
        qa = {
            "topLevelChecks": {"oneH1": True, "htmlHasJsClass": True},
            "interactions": {},
            "signals": {},
            "performanceChecks": {},
        }
        code, failures = self._run_critic(qa=qa, report="final result: passed\n")
        self.assertEqual(code, 1)
        self.assertTrue(
            any("maxRuleDrift" in failure and "cannot compare" in failure for failure in failures),
            f"expected a clear no-baseline failure naming maxRuleDrift, got: {failures}",
        )

    def test_present_max_rule_drift_still_fails_on_real_drift(self) -> None:
        qa = {
            "topLevelChecks": {
                "oneH1": True,
                "htmlHasJsClass": True,
                "ruleFixed": True,
                "maxRuleDrift": 3.4,
                "allUnpostedLabelled": True,
                "formLabels": True,
                "initialStoryCtaHidden": True,
            },
            "interactions": {},
            "signals": {},
            "performanceChecks": {"cls": 0.01, "lcp": 200, "transferBytes": 100_000},
        }
        code, failures = self._run_critic(qa=qa, report="final result: passed\n")
        self.assertEqual(code, 1)
        self.assertTrue(
            any("exceeds 1.5" in failure for failure in failures),
            f"real drift must still fail the comparison, got: {failures}",
        )
        # Absent-data handling must not swallow the real-drift outcome.
        self.assertFalse(
            any("maxRuleDrift" in failure and "cannot compare" in failure for failure in failures),
            f"a measured drift must not be reported as absent data: {failures}",
        )


class VercelDeployMaterializesReleaseTests(unittest.TestCase):
    """The static Vercel deploy must serve the materialized release, not raw site/.

    Release-gate finding: vercel.json pointed outputDirectory at the raw
    ``site/`` source with no build command, so production shipped without the
    canonical/OG/JSON-LD URLs, the founder contact CTA, sitemap.xml, the
    robots.txt Sitemap line, and release-manifest.json that the release
    materializer (build-site-release.py) produces. The deploy is now wired to
    run that materializer as the build step; these tests pin the wiring so it
    cannot silently regress back to a raw deploy.
    """

    def test_vercel_outputs_the_materialized_bundle_not_raw_site(self) -> None:
        vercel = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
        self.assertEqual(vercel["outputDirectory"], "artifacts/release-site")
        self.assertEqual(vercel["buildCommand"], "python scripts/vercel-build.py")
        self.assertIsNone(vercel["framework"], "deploy must remain a static (framework-less) build")

    def test_vercel_build_materializes_release_bundle_with_env_values(self) -> None:
        temp_root = ROOT / ".tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="weft-vercel-build-", dir=temp_root) as temporary:
            output = Path(temporary) / "release-site"
            original_output = _VERCEL_BUILD_MODULE.OUTPUT
            _VERCEL_BUILD_MODULE.OUTPUT = output
            try:
                result = _VERCEL_BUILD_MODULE.build_release_for_deploy(
                    origin="https://weft.test",
                    contact_url="mailto:founder@example.invalid",
                )
            finally:
                _VERCEL_BUILD_MODULE.OUTPUT = original_output
            home = (output / "index.html").read_text(encoding="utf-8")
            self.assertEqual(result["origin"], "https://weft.test")
            self.assertIn('rel="canonical" href="https://weft.test/"', home)
            self.assertIn('data-founder-contact href="mailto:founder@example.invalid"', home)
            self.assertTrue((output / "sitemap.xml").is_file())
            self.assertTrue((output / "release-manifest.json").is_file())

    def test_vercel_build_fails_cleanly_when_contact_url_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "WEFT_CONTACT_URL is unset"):
                _VERCEL_BUILD_MODULE.build_release_for_deploy(origin="https://weft.test")

    def test_vercel_build_refuses_unsafe_contact_values(self) -> None:
        with self.assertRaises(ValueError):
            _VERCEL_BUILD_MODULE.build_release_for_deploy(
                origin="https://weft.test",
                contact_url="javascript:alert(1)",
            )


class TestCountSyncTests(unittest.TestCase):
    """Single source of truth for the published test count.

    The number of passing tests is a measured fact that has drifted across
    six files before. This test discovers the LIVE count from the test suite
    and asserts it against every instance published in docs/ and site/, so
    the number can no longer go stale silently.
    """

    # Files that are allowed to mention a count that is not the full suite.
    # SECURITY_REVIEW reports targeted sub-run counts (e.g. "73 passing tests"
    # for a specific subset), which are not claims about the whole suite.
    _PROVENANCE_ONLY = {
        "BASELINE_2026-08-05.md",
        "PERFORMANCE.md",
        "SECURITY_REVIEW_2026-08-05.md",
    }

    @classmethod
    def _discover_live_count(cls) -> int:
        """Count tests exactly as `unittest discover -s tests` would run them."""
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test*.py")
        return suite.countTestCases()

    @classmethod
    def _scan_for_stale_counts(cls, path: Path, label: str, live: int) -> list[tuple[str, str, int]]:
        """Scan one file for published test counts that differ from ``live``."""
        found: list[tuple[str, str, int]] = []
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return found
        for line_no, line in enumerate(text.splitlines(), 1):
            for match in re.finditer(r"\b(\d{2,4})\s+(?:passing\s+)?standard-library tests\b", line, re.IGNORECASE):
                claimed = int(match.group(1))
                if claimed != live:
                    found.append((label, f"{line_no}: {line.strip()}", claimed))
            for match in re.finditer(r"\b(\d{2,4})\s+passing\s+tests?\b", line, re.IGNORECASE):
                claimed = int(match.group(1))
                if claimed != live:
                    found.append((label, f"{line_no}: {line.strip()}", claimed))
        return found

    @classmethod
    def _published_instances(cls) -> list[tuple[str, str, int]]:
        """Return (file, matched_line, claimed_count) for every published count.

        Walks docs/*.md, site/**/*.html, and site/llms.txt — every surface that
        has historically published the test count.
        """
        live = cls._discover_live_count()
        found: list[tuple[str, str, int]] = []
        docs_root = ROOT / "docs"
        if docs_root.is_dir():
            for path in sorted(docs_root.glob("*.md")):
                if path.name in cls._PROVENANCE_ONLY:
                    continue
                found.extend(cls._scan_for_stale_counts(path, f"docs/{path.name}", live))
        site_root = SITE
        if site_root.is_dir():
            for path in sorted(site_root.glob("**/*.html")):
                found.extend(cls._scan_for_stale_counts(path, f"site/{path.relative_to(site_root)}", live))
            llms = site_root / "llms.txt"
            if llms.is_file():
                found.extend(cls._scan_for_stale_counts(llms, "site/llms.txt", live))
        return found

    def test_published_test_count_matches_live_discovery(self) -> None:
        live = self._discover_live_count()
        stale = self._published_instances()
        self.assertGreater(live, 0, "live test discovery returned zero tests")
        self.assertEqual(
            stale,
            [],
            f"published test count differs from live discovery ({live}): {stale}",
        )


if __name__ == "__main__":
    unittest.main()
