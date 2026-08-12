"""No-install local server for the Weft launch site."""

from __future__ import annotations

import argparse
import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
from weft_cloud.web.security_headers import security_headers


class QuietSiteHandler(SimpleHTTPRequestHandler):
    server_version = "WeftSite/0.1"

    def translate_path(self, path: str) -> str:
        """Mount the self-contained static launch site at /."""
        root = Path(self.directory or os.curdir).resolve()
        route = unquote(urlsplit(path).path)
        if route in {"/", "/index.html"}:
            relative = Path("site/index.html")
            allowed_root = root / "site"
        elif route in {
            "/styles.css",
            "/app.js",
            "/demo.css",
            "/demo-stage.js",
            "/demo.html",
            "/demo-stage.html",
            "/robots.txt",
            "/sitemap.xml",
            "/favicon.ico",
            "/llms.txt",
            "/404.html",
            "/license.html",
            "/terms.html",
            "/privacy.html",
            "/site.webmanifest",
        } or route.startswith(("/assets/", "/blog/", "/docs/", "/_astro/")):
            relative = Path("site") / route.lstrip("/")
            allowed_root = root / "site"
        else:
            relative = Path("site") / "__not_found__"
            allowed_root = root / "site"
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(allowed_root.resolve()):
            return str(root / "site" / "__not_found__")
        return str(candidate)

    def guess_type(self, path: str) -> str:
        """Pin crawler-facing content types so platform mimetypes cannot
        serve a sitemap as text/xml. ``send_head`` passes the translated
        filesystem path, so the decision is based on the request URL."""
        route = urlsplit(self.path).path
        if route == "/sitemap.xml":
            return "application/xml; charset=utf-8"
        if route == "/robots.txt":
            return "text/plain; charset=utf-8"
        return super().guess_type(path)

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Serve the branded static 404 while preserving the HTTP status."""
        if code != 404:
            super().send_error(code, message, explain)
            return
        root = Path(self.directory or os.curdir).resolve()
        body = (root / "site" / "404.html").read_bytes()
        self.send_response(404, message or "Not Found")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def end_headers(self) -> None:
        route = urlsplit(self.path).path
        if route.startswith(("/assets/", "/_astro/")) or route in {"/styles.css", "/app.js"}:
            self.send_header("Cache-Control", "public, max-age=3600")
        else:
            self.send_header("Cache-Control", "no-store")
        # Full security header block (HSTS, CSP, frame protection, referrer,
        # Permissions-Policy). CSP is harmless on the site server's CSS/JS
        # assets and mandatory on its HTML pages.
        for name, value in security_headers(html=True):
            self.send_header(name, value)
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the dependency-free Weft launch site")
    parser.add_argument("--host", default=os.environ.get("WEFT_SITE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEFT_SITE_PORT", "4173")))
    parser.add_argument("--site-root", default=os.environ.get("WEFT_SITE_ROOT"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.site_root:
        configured_root = Path(args.site_root).expanduser().resolve()
        root = configured_root.parent if configured_root.name.lower() == "site" and (configured_root / "index.html").is_file() else configured_root
    else:
        root = Path(__file__).resolve().parents[1]
    if not (root / "site").is_dir() or not (root / "site" / "index.html").is_file():
        raise SystemExit(f"Weft project root does not contain site/index.html: {root}")
    handler = partial(QuietSiteHandler, directory=str(root))

    class SiteServer(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = SiteServer((args.host, args.port), handler)
    print(f"Weft site listening on http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
