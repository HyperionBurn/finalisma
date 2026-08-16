from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts import probe_live_release


class _ProbeHandler(BaseHTTPRequestHandler):
    surface = "api"
    drift = False

    def _headers(self, *, site: bool = False) -> None:
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if not site or not self.drift:
            self.send_header("Content-Security-Policy", "default-src 'self'")
        self.end_headers()

    def _reply(self, status: int, body: bytes = b"", *, location: str | None = None, site: bool = False) -> None:
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._headers(site=site)
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.surface == "api":
            if self.path == "/healthz":
                self._reply(200, b'{"status":"ok"}')
            elif self.path == "/":
                self._reply(303, location="/login")
            elif self.path == "/login":
                self._reply(200, b"login")
            else:
                self._reply(404)
            return

        if self.path == "/" and not self.drift:
            self._reply(
                200,
                b'<link rel="canonical" href="https://example.test/">'
                b'<meta property="og:url" content="https://example.test/">'
                b'<button data-cohort-build>Build</button>',
                site=True,
            )
        elif self.path == "/" and self.drift:
            self._reply(200, b"old bundle", site=True)
        elif self.path == "/robots.txt" and not self.drift:
            self._reply(200, b"User-agent: *\nSitemap: https://example.test/sitemap.xml\n", site=True)
        elif self.path == "/robots.txt":
            self._reply(200, b"User-agent: *\nAllow: /\n", site=True)
        elif self.path == "/sitemap.xml" and not self.drift:
            self._reply(200, b"<urlset></urlset>", site=True)
        elif self.path == "/release-manifest.json" and not self.drift:
            self._reply(200, b"{}", site=True)
        else:
            self._reply(404, site=True)

    def log_message(self, *_args) -> None:
        return


def _serve(surface: str, drift: bool = False) -> tuple[ThreadingHTTPServer, str]:
    handler = type(
        f"{surface.title()}ProbeHandler",
        (_ProbeHandler,),
        {"surface": surface, "drift": drift},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class LiveReleaseProbeTests(unittest.TestCase):
    def test_pass_and_reachable_drift_are_distinct(self) -> None:
        api_server, api_origin = _serve("api")
        site_server, site_origin = _serve("site")
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
        finally:
            api_server.shutdown()
            site_server.shutdown()
            api_server.server_close()
            site_server.server_close()

        api_server, api_origin = _serve("api")
        site_server, site_origin = _serve("site", drift=True)
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


if __name__ == "__main__":
    unittest.main()
