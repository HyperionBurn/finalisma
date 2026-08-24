from __future__ import annotations
from tests._server_readiness import await_serving as _await_serving

import hashlib
import io
import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from scripts import probe_live_release


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class _ProbeHandler(BaseHTTPRequestHandler):
    surface = "api"
    drift = False
    storage_ready = True
    readiness_service = "weft-cloud"
    api_origin = ""

    def _headers(self, *, site: bool = False) -> None:
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not site or not self.drift:
            self.send_header("Content-Security-Policy", "default-src 'self'")
        self.end_headers()

    def _reply(
        self,
        status: int,
        body: bytes = b"",
        *,
        content_type: str = "text/plain; charset=utf-8",
        location: str | None = None,
        site: bool = False,
    ) -> None:
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._headers(site=site)
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.surface == "api":
            if self.path == "/healthz":
                self._reply(200, b'{"status":"ok"}')
            elif self.path in {"/readyz", "/v1/readyz"}:
                if self.storage_ready:
                    body = json.dumps({"status": "ready", "service": self.readiness_service}).encode()
                    self._reply(200, body, content_type="application/json")
                else:
                    body = json.dumps({"status": "unavailable", "service": self.readiness_service}).encode()
                    self._reply(503, body, content_type="application/json")
            elif self.path == "/":
                self._reply(303, location="/login")
            elif self.path in {"/login", "/signup"}:
                self._reply(200, b"login")
            else:
                self._reply(404)
            return

        if self.drift and self.path == "/":
            self._reply(200, b"old bundle", site=True)
        elif self.path == "/" and not self.drift:
            self._reply(
                200,
                (
                    f'<link rel="canonical" href="{self.server.site_origin}/">'
                    f'<meta property="og:url" content="{self.server.site_origin}/">'
                    '<meta property="og:site_name" content="Weft">'
                    '<meta property="og:title" content="Weft home">'
                    '<meta property="og:description" content="Governed agent rooms.">'
                    '<meta property="og:image" content="/assets/weft-demo-poster.png">'
                    '<meta property="og:image:alt" content="Weft social card.">'
                    '<meta name="twitter:card" content="summary_large_image">'
                    '<meta name="twitter:title" content="Weft home">'
                    '<meta name="twitter:description" content="Governed agent rooms.">'
                    '<meta name="twitter:image" content="/assets/weft-demo-poster.png">'
                    '<meta name="twitter:image:alt" content="Weft social card.">'
                    f'<a href="{self.api_origin}/signup">Build</a>'
                    '<button data-cohort-build>Build</button>'
                ).encode(),
                content_type="text/html; charset=utf-8",
                site=True,
            )
        elif self.path in {"/docs", "/docs/quickstart", "/demo"} and not self.drift:
            expected_urls = {
                "/docs": f"{self.server.site_origin}/docs/index.html",
                "/docs/quickstart": f"{self.server.site_origin}/docs/quickstart.html",
                "/demo": f"{self.server.site_origin}/demo.html",
            }
            expected_url = expected_urls[self.path]
            body = (
                f'<link rel="canonical" href="{expected_url}">'
                f'<meta property="og:url" content="{expected_url}">'
                '<meta property="og:site_name" content="Weft">'
                '<meta property="og:title" content="Weft page">'
                '<meta property="og:description" content="Governed agent rooms.">'
                '<meta property="og:image" content="/assets/weft-demo-poster.png">'
                '<meta property="og:image:alt" content="Weft social card.">'
                '<meta name="twitter:card" content="summary_large_image">'
                '<meta name="twitter:title" content="Weft page">'
                '<meta name="twitter:description" content="Governed agent rooms.">'
                '<meta name="twitter:image" content="/assets/weft-demo-poster.png">'
                '<meta name="twitter:image:alt" content="Weft social card.">'
            )
            if self.path == "/demo":
                body += (
                    '<video poster="/assets/weft-demo-poster.png">'
                    '<source src="/assets/weft-demo.mp4" type="video/mp4">'
                    '<source src="/assets/weft-demo.webm" type="video/webm">'
                    '<track default kind="captions" srclang="en" src="/assets/weft-demo.vtt">'
                    '</video>'
                )
            self._reply(
                200,
                body.encode(),
                content_type="text/html; charset=utf-8",
                site=True,
            )
        elif self.path == "/404" and not self.drift:
            self._reply(
                404,
                b"This path is not in the account.",
                content_type="text/html; charset=utf-8",
                site=True,
            )
        elif self.path == "/robots.txt" and not self.drift:
            self._reply(
                200,
                f"User-agent: *\nSitemap: {self.server.site_origin}/sitemap.xml\n".encode(),
                site=True,
            )
        elif self.path == "/sitemap.xml" and not self.drift:
            self._reply(
                200,
                (
                    f"<urlset><url><loc>{self.server.site_origin}/</loc></url>"
                    f"<url><loc>{self.server.site_origin}/docs/index.html</loc></url>"
                    f"<url><loc>{self.server.site_origin}/docs/quickstart.html</loc></url>"
                    f"<url><loc>{self.server.site_origin}/demo.html</loc></url></urlset>"
                ).encode(),
                content_type="application/xml",
                site=True,
            )
        elif self.path == "/release-manifest.json" and not self.drift:
            manifest = {
                "schema": "weft.site-release/v1",
                "origin": self.server.site_origin,
                "page_count": 4,
                "sitemap_url_count": 4,
                "media_sha256": {
                    "weft-demo.mp4": _sha256(b"mp4-fixture"),
                    "weft-demo.webm": _sha256(b"webm-fixture"),
                    "weft-demo.vtt": _sha256(b"WEBVTT\n"),
                    "weft-demo-poster.png": _sha256(b"png-fixture"),
                },
            }
            self._reply(
                200,
                json.dumps(manifest).encode(),
                content_type="application/json",
                site=True,
            )
        elif self.path == "/assets/weft-demo.mp4" and not self.drift:
            self._reply(200, b"mp4-fixture", content_type="video/mp4", site=True)
        elif self.path == "/assets/weft-demo.webm" and not self.drift:
            self._reply(200, b"webm-fixture", content_type="video/webm", site=True)
        elif self.path == "/assets/weft-demo.vtt" and not self.drift:
            self._reply(200, b"WEBVTT\n", content_type="text/vtt", site=True)
        elif self.path == "/assets/weft-demo-poster.png" and not self.drift:
            self._reply(200, b"png-fixture", content_type="image/png", site=True)
        elif self.path == "/robots.txt":
            self._reply(200, b"User-agent: *\nAllow: /\n", site=True)
        elif self.path == "/sitemap.xml" and self.drift:
            self._reply(200, b"<urlset></urlset>", content_type="application/xml", site=True)
        elif self.path == "/release-manifest.json" and self.drift:
            self._reply(200, b"{}", content_type="application/json", site=True)
        else:
            self._reply(404, site=True)

    def log_message(self, *_args) -> None:
        return


def _serve(
    surface: str,
    drift: bool = False,
    *,
    api_origin: str = "",
    storage_ready: bool = True,
    readiness_service: str = "weft-cloud",
) -> tuple[ThreadingHTTPServer, str]:
    handler = type(
        f"{surface.title()}ProbeHandler",
        (_ProbeHandler,),
        {
            "surface": surface,
            "drift": drift,
            "storage_ready": storage_ready,
            "readiness_service": readiness_service,
            "api_origin": api_origin,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.site_origin = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _await_serving(server)
    return server, server.site_origin


class LiveReleaseProbeTests(unittest.TestCase):
    def test_cli_requires_explicit_origins_instead_of_stale_defaults(self) -> None:
        output = io.StringIO()
        with patch.dict(os.environ, {"WEFT_API_ORIGIN": "", "WEFT_SITE_URL": ""}, clear=False):
            with redirect_stdout(output):
                code = probe_live_release.main([])

        result = json.loads(output.getvalue())
        self.assertEqual(code, 4)
        self.assertEqual(result["status"], "INVALID_ARGUMENT")
        self.assertIn("WEFT_API_ORIGIN", result["error"])
        self.assertIn("WEFT_SITE_URL", result["error"])

    def test_pass_and_reachable_drift_are_distinct(self) -> None:
        api_server, api_origin = _serve("api")
        site_server, site_origin = _serve("site", api_origin=api_origin)
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                code = probe_live_release.main(
                    ["--api-origin", api_origin, "--site-origin", site_origin]
                )
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["checks"]["release_alignment"])
            self.assertTrue(result["checks"]["api_readiness"])
        finally:
            api_server.shutdown()
            site_server.shutdown()
            api_server.server_close()
            site_server.server_close()

        api_server, api_origin = _serve("api")
        site_server, site_origin = _serve("site", drift=True, api_origin=api_origin)
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                code = probe_live_release.main(
                    ["--api-origin", api_origin, "--site-origin", site_origin]
                )
            result = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "DRIFT")
            self.assertTrue(result["checks"]["reachability"])
            self.assertFalse(result["checks"]["release_alignment"])
            self.assertNotIn("old bundle", output.getvalue())
        finally:
            api_server.shutdown()
            site_server.shutdown()
            api_server.server_close()
            site_server.server_close()

    def test_storage_readiness_failure_blocks_release_even_when_liveness_is_ok(self) -> None:
        api_server, api_origin = _serve("api", storage_ready=False)
        site_server, site_origin = _serve("site", api_origin=api_origin)
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                code = probe_live_release.main(
                    ["--api-origin", api_origin, "--site-origin", site_origin]
                )

            result = json.loads(output.getvalue())
            self.assertEqual(code, 3)
            self.assertEqual(result["status"], "UNREACHABLE")
            self.assertTrue(result["checks"]["api_health"])
            self.assertFalse(result["checks"]["api_readiness"])
            self.assertFalse(result["checks"]["reachability"])
            self.assertTrue(
                any(item["check"] == "api_readiness" for item in result["diagnostics"])
            )
            self.assertNotIn('"status":"unavailable"', output.getvalue())
        finally:
            api_server.shutdown()
            site_server.shutdown()
            api_server.server_close()
            site_server.server_close()

    def test_readiness_requires_ready_marker_not_only_http_200(self) -> None:
        response = {"status": 200, "body": '{"status":"unavailable"}'}
        self.assertFalse(probe_live_release._json_status(response, "ready"))

    def test_readiness_requires_cloud_service_marker(self) -> None:
        api_server, api_origin = _serve("api", readiness_service="weft-web")
        site_server, site_origin = _serve("site", api_origin=api_origin)
        try:
            result = probe_live_release.probe(api_origin, site_origin, timeout=2)
            self.assertEqual(result["status"], "UNREACHABLE")
            self.assertFalse(result["checks"]["api_readiness"])
            self.assertFalse(result["checks"]["reachability"])
        finally:
            api_server.shutdown()
            site_server.shutdown()
            api_server.server_close()
            site_server.server_close()


if __name__ == "__main__":
    unittest.main()
