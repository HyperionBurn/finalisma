from __future__ import annotations

import hashlib
import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts import probe_live_release


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class _ContractHandler(BaseHTTPRequestHandler):
    origin = ""
    api_origin = ""
    surface = "site"
    mode = "pass"
    mutation_attempts = 0

    def _send_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'",
        )

    def _reply(
        self,
        status: int,
        body: bytes = b"",
        *,
        content_type: str = "text/html; charset=utf-8",
        location: str | None = None,
    ) -> None:
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self._send_headers(content_type)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.surface == "api":
            if self.path == "/healthz":
                self._reply(200, b'{"status":"ok"}', content_type="application/json")
            elif self.path == "/readyz":
                self._reply(200, b'{"status":"ready"}', content_type="application/json")
            elif self.path == "/":
                self._reply(303, location="/login")
            elif self.path == "/login":
                self._reply(200, b"<title>Login</title>")
            elif self.path == "/signup":
                self._reply(200, b"<title>Signup</title>")
            else:
                self._reply(404, b"not found")
            return
        self._site_get()

    def do_POST(self) -> None:  # noqa: N802
        type(self).mutation_attempts += 1
        self._reply(405, b"mutation forbidden", content_type="text/plain; charset=utf-8")

    def _site_get(self) -> None:
        pages = {
            "/": (
                200,
                f'<link rel="canonical" href="{self.origin}/">'
                f'<meta property="og:url" content="{self.origin}/">'
                '<meta property="og:site_name" content="Weft">'
                '<meta property="og:title" content="Weft home">'
                '<meta property="og:description" content="Governed agent rooms.">'
                '<meta property="og:image" content="/assets/og-card.png">'
                '<meta property="og:image:alt" content="Weft social card.">'
                '<meta name="twitter:card" content="summary_large_image">'
                '<meta name="twitter:title" content="Weft home">'
                '<meta name="twitter:description" content="Governed agent rooms.">'
                '<meta name="twitter:image" content="/assets/og-card.png">'
                '<meta name="twitter:image:alt" content="Weft social card.">'
                f'<button data-cohort-build>Build</button><a href="{self.api_origin}/signup">Open a room</a>',
            ),
            "/docs": (
                200,
                f'<link rel="canonical" href="{self.origin}/docs/index.html">'
                f'<meta property="og:url" content="{self.origin}/docs/index.html">'
                '<meta property="og:site_name" content="Weft">'
                '<meta property="og:title" content="Weft docs">'
                '<meta property="og:description" content="Governed agent room docs.">'
                '<meta property="og:image" content="/assets/og-card.png">'
                '<meta property="og:image:alt" content="Weft social card.">'
                '<meta name="twitter:card" content="summary_large_image">'
                '<meta name="twitter:title" content="Weft docs">'
                '<meta name="twitter:description" content="Governed agent room docs.">'
                '<meta name="twitter:image" content="/assets/og-card.png">'
                '<meta name="twitter:image:alt" content="Weft social card.">',
            ),
            "/docs/quickstart": (
                200,
                f'<link rel="canonical" href="{self.origin}/docs/quickstart.html">'
                f'<meta property="og:url" content="{self.origin}/docs/quickstart.html">'
                '<meta property="og:site_name" content="Weft">'
                '<meta property="og:title" content="Weft quickstart">'
                '<meta property="og:description" content="Open a governed room.">'
                '<meta property="og:image" content="/assets/og-card.png">'
                '<meta property="og:image:alt" content="Weft social card.">'
                '<meta name="twitter:card" content="summary_large_image">'
                '<meta name="twitter:title" content="Weft quickstart">'
                '<meta name="twitter:description" content="Open a governed room.">'
                '<meta name="twitter:image" content="/assets/og-card.png">'
                '<meta name="twitter:image:alt" content="Weft social card.">',
            ),
            "/demo": (
                200,
                f'<link rel="canonical" href="{self.origin}/demo.html">'
                f'<meta property="og:url" content="{self.origin}/demo.html">'
                '<meta property="og:site_name" content="Weft">'
                '<meta property="og:title" content="Weft demo">'
                '<meta property="og:description" content="Recorded proof.">'
                '<meta property="og:image" content="/assets/weft-demo-poster.png">'
                '<meta property="og:image:alt" content="Weft social card.">'
                '<meta name="twitter:card" content="summary_large_image">'
                '<meta name="twitter:title" content="Weft demo">'
                '<meta name="twitter:description" content="Recorded proof.">'
                '<meta name="twitter:image" content="/assets/weft-demo-poster.png">'
                '<meta name="twitter:image:alt" content="Weft social card.">'
                '<video poster="assets/weft-demo-poster.png">'
                '<source src="assets/weft-demo.mp4" type="video/mp4">'
                '<source src="assets/weft-demo.webm" type="video/webm">'
                '<track default kind="captions" srclang="en" src="assets/weft-demo.vtt">'
                '</video>',
            ),
        }
        if self.path in pages:
            status, body = pages[self.path]
            if self.mode == "page-drift" and self.path == "/docs/quickstart":
                body = "old release SECRET_BODY"
            self._reply(status, body.encode())
            return
        if self.path == "/404":
            self._reply(404, b"This path is not in the account.")
            return
        if self.path == "/robots.txt":
            self._reply(
                200,
                f"User-agent: *\nSitemap: {self.origin}/sitemap.xml\n".encode(),
                content_type="text/plain; charset=utf-8",
            )
            return
        if self.path == "/sitemap.xml":
            self._reply(
                200,
                f"<urlset><url><loc>{self.origin}/</loc></url>"
                f"<url><loc>{self.origin}/docs/index.html</loc></url>"
                f"<url><loc>{self.origin}/docs/quickstart.html</loc></url>"
                f"<url><loc>{self.origin}/demo.html</loc></url></urlset>".encode(),
                content_type="application/xml",
            )
            return
        if self.path == "/release-manifest.json":
            manifest = {
                "schema": "weft.site-release/v1",
                "origin": self.origin,
                "contact_scheme": "mailto",
                "page_count": 4,
                "sitemap_url_count": 4,
                "media_sha256": {
                    "weft-demo.mp4": _sha256(b"media"),
                    "weft-demo.webm": _sha256(b"media"),
                    "weft-demo.vtt": _sha256(b"WEBVTT\n"),
                    "weft-demo-poster.png": _sha256(b"media"),
                },
            }
            if self.mode == "manifest-drift":
                manifest["schema"] = "old-release/v1"
            self._reply(
                200,
                json.dumps(manifest).encode(),
                content_type="application/json",
            )
            return
        media = {
            "/assets/weft-demo.mp4": "video/mp4",
            "/assets/weft-demo.webm": "video/webm",
            "/assets/weft-demo.vtt": "text/vtt",
            "/assets/weft-demo-poster.png": "image/png",
        }
        if self.path in media:
            body = b"WEBVTT\n" if self.path.endswith(".vtt") else b"media"
            if self.mode == "media-drift" and self.path == "/assets/weft-demo.mp4":
                body = b"media-drift"
            self._reply(200, body, content_type=media[self.path])
            return
        self._reply(404, b"not found")

    def log_message(self, *_args) -> None:
        return


def _serve(mode: str = "pass", *, surface: str = "site") -> tuple[ThreadingHTTPServer, str]:
    handler = type(
        "ContractProbeHandler",
        (_ContractHandler,),
        {"mode": mode, "surface": surface, "mutation_attempts": 0},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    handler.origin = origin
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, origin


def _serve_pair(mode: str = "pass") -> tuple[ThreadingHTTPServer, str, ThreadingHTTPServer, str]:
    api_server, api_origin = _serve(surface="api")
    site_server, site_origin = _serve(mode)
    site_server.RequestHandlerClass.origin = site_origin
    site_server.RequestHandlerClass.api_origin = api_origin
    return api_server, api_origin, site_server, site_origin


class LiveReleaseProbeContractTests(unittest.TestCase):
    def test_diagnostic_endpoint_facts_redact_redirect_queries_and_transport_errors(self) -> None:
        redirect = probe_live_release._safe_endpoint_facts(
            {
                "status": 302,
                "headers": {
                    "content-type": "text/html",
                    "location": "https://user:pass@example.test/login?token=secret#fragment",
                },
                "bytes": 0,
                "truncated": False,
            }
        )
        self.assertEqual(redirect["location"], "https://example.test/login")
        self.assertNotIn("secret", json.dumps(redirect))
        self.assertNotIn("user", json.dumps(redirect))

        transport = probe_live_release._safe_endpoint_facts(
            {
                "status": None,
                "headers": {},
                "bytes": 0,
                "truncated": False,
                "error": "https://example.test/?token=secret: connection refused",
            }
        )
        self.assertEqual(transport["error"], "transport_error")
        self.assertNotIn("secret", json.dumps(transport))

    def test_pass_covers_extended_release_contract_without_mutation(self) -> None:
        api_server, api_origin, site_server, site_origin = _serve_pair()
        try:
            result = probe_live_release.probe(api_origin, site_origin, timeout=2)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["diagnostics"], [])
            self.assertNotIn("sha256", json.dumps(result))
            for check in (
                "api_readiness",
                "site_docs_reachable",
                "site_quickstart_reachable",
                "site_demo_reachable",
                "site_404_content",
                "site_signup_cta_target",
                "site_canonical_og_metadata",
                "site_demo_captions_media",
                "site_indexing",
                "site_release_manifest",
                "site_security_headers",
                "release_alignment",
            ):
                with self.subTest(check=check):
                    self.assertTrue(result["checks"][check])
            self.assertEqual(api_server.RequestHandlerClass.mutation_attempts, 0)
            self.assertEqual(site_server.RequestHandlerClass.mutation_attempts, 0)
        finally:
            for server in (api_server, site_server):
                server.shutdown()
                server.server_close()

    def test_missing_page_contract_is_reachable_drift_and_body_is_redacted(self) -> None:
        api_server, api_origin, site_server, site_origin = _serve_pair("page-drift")
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                code = probe_live_release.main(
                    ["--api-origin", api_origin, "--site-origin", site_origin, "--timeout", "2"]
                )
            result = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "DRIFT")
            self.assertTrue(result["checks"]["reachability"])
            self.assertFalse(result["checks"]["site_canonical_og_metadata"])
            diagnostics = {item["check"]: item for item in result["diagnostics"]}
            self.assertIn("site_canonical_og_metadata", diagnostics)
            self.assertIn("site_quickstart", diagnostics["site_canonical_og_metadata"]["endpoints"])
            self.assertNotIn("SECRET_BODY", output.getvalue())
            self.assertEqual(api_server.RequestHandlerClass.mutation_attempts, 0)
            self.assertEqual(site_server.RequestHandlerClass.mutation_attempts, 0)
        finally:
            for server in (api_server, site_server):
                server.shutdown()
                server.server_close()

    def test_manifest_drift_does_not_become_unreachable(self) -> None:
        api_server, api_origin, site_server, site_origin = _serve_pair("manifest-drift")
        try:
            result = probe_live_release.probe(api_origin, site_origin, timeout=2)
            self.assertEqual(result["status"], "DRIFT")
            self.assertTrue(result["checks"]["reachability"])
            self.assertFalse(result["checks"]["site_release_manifest"])
            diagnostics = {item["check"]: item for item in result["diagnostics"]}
            self.assertIn("site_release_manifest", diagnostics)
            self.assertIn("site_manifest", diagnostics["site_release_manifest"]["endpoints"])
        finally:
            for server in (api_server, site_server):
                server.shutdown()
                server.server_close()

    def test_media_byte_drift_fails_release_alignment(self) -> None:
        api_server, api_origin, site_server, site_origin = _serve_pair("media-drift")
        try:
            result = probe_live_release.probe(api_origin, site_origin, timeout=2)
            self.assertEqual(result["status"], "DRIFT")
            self.assertTrue(result["checks"]["reachability"])
            self.assertFalse(result["checks"]["site_release_manifest"])
            self.assertFalse(result["checks"]["release_alignment"])
            self.assertEqual(api_server.RequestHandlerClass.mutation_attempts, 0)
            self.assertEqual(site_server.RequestHandlerClass.mutation_attempts, 0)
        finally:
            for server in (api_server, site_server):
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
