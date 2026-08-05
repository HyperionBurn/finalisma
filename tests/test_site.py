from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
sys.path.insert(0, str(ROOT))

_SITE_SPEC = importlib.util.spec_from_file_location(
    "finalisma_site", ROOT / "scripts" / "finalisma-site.py"
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


class LaunchSurfaceTests(unittest.TestCase):
    def test_landing_page_has_truthful_semantic_launch_surface(self) -> None:
        html = (SITE / "index.html").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"<h1[\s>]", html)), 1)
        self.assertIn("One incident. Two agents. One account of what happened.", html)
        self.assertIn("9 documented MCP paths", html)
        self.assertIn("0 verified Finalisma host integrations", html)
        self.assertIn("first proof pair: Codex + Claude Code", html)
        self.assertIn('type="application/ld+json"', html)
        self.assertIn('property="og:site_name" content="Finalisma"', html)
        self.assertIn('name="twitter:image" content="/assets/og-card.png"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("data-sim-label", html)
        self.assertIn("Simulated account · no credentials · no live session", html)
        self.assertIn("MCP is the tool protocol", html)
        self.assertIn("single-node", html.lower())
        self.assertIn("data-cohort-form", html)
        self.assertIn("Watch the 42-second proof", html)
        self.assertIn('href="/demo.html"', html)
        self.assertIn("$500 deposit", html)
        self.assertNotIn("verified agent handoff layer", html.lower())
        self.assertNotIn("Finalisma A2A Standard", html)
        self.assertNotIn("gpt-5.5", html.lower())
        self.assertNotIn("lorem ipsum", html.lower())

    def test_progressive_enhancement_and_gated_story_cta(self) -> None:
        html = (SITE / "index.html").read_text(encoding="utf-8")
        css = (SITE / "styles.css").read_text(encoding="utf-8")
        self.assertIn("document.documentElement.classList.add('js')", html)
        self.assertIn("[hidden] { display: none !important; }", css)
        self.assertIn(".reveal { opacity: 1; transform: none; }", css)
        self.assertIn(".js .reveal", css)
        self.assertRegex(html, r"data-story-next[^>]*hidden")
        self.assertIn("data-recon-plane", html)
        self.assertIn("perspective: 1500px", css)
        self.assertIn("transform-style: preserve-3d", css)

    def test_unposted_entries_never_rely_on_colour_alone(self) -> None:
        html = (SITE / "index.html").read_text(encoding="utf-8")
        entries = re.findall(
            r'<li class="entry(?![^"]*is-posted)[^"]*"[^>]*>(.*?)</li>', html, re.S
        )
        self.assertGreaterEqual(len(entries), 11)
        for entry in entries:
            with self.subTest(entry=entry[:60]):
                state = re.search(r'<span class="entry-state">([^<]+)</span>', entry)
                self.assertIsNotNone(state, "unposted entry has no textual state marker")
                self.assertTrue(state.group(1).strip())

    def test_static_launch_bundle_contains_guides_articles_and_social_asset(self) -> None:
        required_root = {
            "index.html",
            "styles.css",
            "app.js",
            "robots.txt",
            "llms.txt",
            "404.html",
            "license.html",
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
        }
        self.assertEqual(required_guides, {path.name for path in (SITE / "docs").glob("*.html")})

        og_card = SITE / "assets" / "og-card.png"
        self.assertTrue(og_card.is_file())
        png_header = og_card.read_bytes()
        self.assertEqual(png_header[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(png_header[16:20], "big"), 1200)
        self.assertEqual(int.from_bytes(png_header[20:24], "big"), 630)

        poster = SITE / "assets" / "finalisma-demo-poster.png"
        poster_header = poster.read_bytes()
        self.assertEqual(poster_header[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(poster_header[16:20], "big"), 1280)
        self.assertEqual(int.from_bytes(poster_header[20:24], "big"), 720)

        mp4 = SITE / "assets" / "finalisma-demo.mp4"
        webm = SITE / "assets" / "finalisma-demo.webm"
        captions = SITE / "assets" / "finalisma-demo.vtt"
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
        with tempfile.TemporaryDirectory(prefix="finalisma-release-test-", dir=temp_root) as temporary:
            output = Path(temporary) / "site"
            result = _RELEASE_MODULE.build_release(
                origin="https://finalisma.test",
                contact_url="mailto:founder@example.invalid",
                output=output,
            )
            built_home = (output / "index.html").read_text(encoding="utf-8")
            built_demo = (output / "demo.html").read_text(encoding="utf-8")
            self.assertEqual(result["origin"], "https://finalisma.test")
            self.assertIn('href="https://finalisma.test/"', built_home)
            self.assertIn('property="og:url" content="https://finalisma.test/"', built_home)
            self.assertIn('name="twitter:image" content="https://finalisma.test/assets/og-card.png"', built_home)
            self.assertIn('data-founder-contact href="mailto:founder@example.invalid"', built_home)
            self.assertIn('"contentUrl": "https://finalisma.test/assets/finalisma-demo.mp4"', built_demo)
            self.assertIn("https://finalisma.test/demo.html", (output / "sitemap.xml").read_text(encoding="utf-8"))
            self.assertIn("Sitemap: https://finalisma.test/sitemap.xml", (output / "robots.txt").read_text(encoding="utf-8"))
            self.assertTrue((output / "release-manifest.json").is_file())
        for invalid_origin in (
            "http://finalisma.test",
            "https://user:secret@finalisma.test",
            "https://finalisma.test/subpath",
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
                target = (
                    (SITE / href_path.lstrip("/")).resolve()
                    if href_path.startswith("/")
                    else (page.parent / href_path).resolve()
                )
                with self.subTest(page=page.relative_to(ROOT), reference=reference):
                    self.assertTrue(target.is_relative_to(SITE.resolve()))
                    self.assertTrue(target.is_file() or target.is_dir(), f"missing target: {target}")

    def test_compatibility_page_keeps_documented_and_verified_distinct(self) -> None:
        html = (SITE / "docs" / "compatibility.html").read_text(encoding="utf-8")
        self.assertIn("9 documented-unverified", html)
        self.assertIn("0 verified", html)
        self.assertIn("Documented is not verified.", html)
        for host in ("Codex", "ChatGPT", "Claude Code", "VS Code", "Cursor", "Gemini CLI", "OpenCode", "Zed", "Cline"):
            self.assertIn(host, html)

    def test_recorded_demo_is_redacted_and_grounded_in_a_real_run(self) -> None:
        demo = (SITE / "demo.html").read_text(encoding="utf-8")
        transcript = json.loads((SITE / "assets" / "demo-transcript.json").read_text(encoding="utf-8"))
        build = json.loads((ROOT / "artifacts" / "design-qa" / "demo-video-results.json").read_text(encoding="utf-8"))
        self.assertIn("The two agent hosts are deterministic fixtures", demo)
        self.assertIn("finalisma-demo.mp4", demo)
        self.assertIn("finalisma-demo.webm", demo)
        self.assertIn("finalisma-demo.vtt", demo)
        self.assertIn('"@type": "VideoObject"', demo)
        self.assertIn('content="/assets/finalisma-demo.mp4"', demo)
        self.assertIn('content="1280"', demo)
        self.assertIn('content="720"', demo)
        self.assertIn('content="index,follow,max-video-preview:-1,max-image-preview:large"', demo)
        self.assertIn('property="og:site_name" content="Finalisma"', demo)
        self.assertIn('name="twitter:image" content="/assets/finalisma-demo-poster.png"', demo)
        self.assertEqual(transcript["schema"], "finalisma.public-demo/v1")
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
            self.assertIn("One incident. Two agents.", home)

            status, guide, _ = get("/docs/compatibility.html")
            self.assertEqual(status, 200)
            self.assertIn("Documented is not verified.", guide)

            status, demo, _ = get("/demo.html")
            self.assertEqual(status, 200)
            self.assertIn("A real coordinator run.", demo)

            status, manifest, _ = get("/site.webmanifest")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(manifest)["name"], "Finalisma")

            status, _, _ = get("/assets/finalisma-demo.mp4")
            self.assertEqual(status, 200)

            status, _, headers = get("/assets/og-card.png")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Cache-Control"), "public, max-age=3600")

            status, missing, _ = get("/not-a-real-entry")
            self.assertEqual(status, 404)
            self.assertIn("This path is not in the account.", missing)

            for path in ("/../src/finalisma_mcp/core.py", "/docs/%2e%2e/src/finalisma_mcp/core.py"):
                with self.subTest(path=path):
                    status, body, _ = get(path)
                    self.assertEqual(status, 404)
                    self.assertNotIn("class Finalisma", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
