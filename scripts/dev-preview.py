"""Serve the built site/ tree the way Vercel will actually serve it.

A plain `python -m http.server` is a false-negative generator for this project:
it sends no Content-Security-Policy, so the hero video and the webfonts appeared
to work locally for weeks while being blocked outright in production. Every
Lighthouse run, screenshot and content check taken against a plain static server
was measuring a page that could not exist on Vercel.

So this replays vercel.json rather than reimplementing it:

  * headers   - read from vercel.json at startup, including the full CSP, so the
                policy here is by construction the policy that ships. Change
                vercel.json and this changes with it.
  * cleanUrls - /login resolves to login/index.html and /docs/quickstart to
                docs/quickstart.html. Without this, extensionless paths 404 and
                any audit run against them silently measures the error page
                instead of the app.
  * MIME      - woff2 / webp / mp4 are spelled out; Python's default map gets
                these wrong or omits them, which breaks font and media loading
                for reasons that look like CSP failures but are not.

Usage:  python scripts/dev-preview.py [port]
"""
import json
import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
SITE = os.path.join(ROOT, 'site')
VERCEL = os.path.join(ROOT, 'vercel.json')

EXTRA_TYPES = {
    '.woff2': 'font/woff2',
    '.webp': 'image/webp',
    '.mp4': 'video/mp4',
    '.svg': 'image/svg+xml',
    '.webmanifest': 'application/manifest+json',
    '.py': 'text/x-python',
    '.mjs': 'text/javascript',
}


def load_headers():
    """Pull the headers vercel.json applies to every path."""
    with open(VERCEL, encoding='utf-8') as fh:
        cfg = json.load(fh)
    out = []
    for rule in cfg.get('headers', []):
        if rule.get('source') in ('/(.*)', '/(.*)/', '/:path*'):
            for h in rule.get('headers', []):
                out.append((h['key'], h['value']))
    return out, bool(cfg.get('cleanUrls'))


HEADERS, CLEAN_URLS = load_headers()


class Handler(SimpleHTTPRequestHandler):
    extensions_map = dict(SimpleHTTPRequestHandler.extensions_map, **EXTRA_TYPES)

    def end_headers(self):
        for key, value in HEADERS:
            self.send_header(key, value)
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def translate_path(self, path):
        local = super().translate_path(path)
        if not CLEAN_URLS:
            return local
        # vercel.json sets trailingSlash:false, so /login must answer 200 in
        # place. Returning the directory instead would let the base handler
        # 301 to /login/ — harmless in a browser, but it means an audit run
        # against extensionless paths measures a redirect chain, or a 404 page,
        # rather than the page itself. That exact gap produced a batch of
        # phantom axe violations once already.
        if os.path.isdir(local):
            index = os.path.join(local, 'index.html')
            return index if os.path.exists(index) else local
        if os.path.exists(local):
            return local
        for candidate in (local + '.html', os.path.join(local, 'index.html')):
            if os.path.exists(candidate):
                return candidate
        return local

    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else ''
        if str(status).startswith(('4', '5')):
            sys.stderr.write("  %s %s\n" % (status, args[0]))


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a SECOND process bind a port that is already
    # listening, instead of failing. Both instances then answer, alternately and
    # invisibly — so a stale server keeps serving old code while the new one
    # looks like it started fine. That cost a confusing debugging round; fail
    # loudly on a taken port instead.
    allow_reuse_address = False
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4180
    handler = partial(Handler, directory=SITE)
    try:
        srv = Server(('127.0.0.1', port), handler)
    except OSError as exc:
        sys.exit("port %d is already in use (%s) — stop the old server first" % (port, exc))
    csp = next((v for k, v in HEADERS if k.lower() == 'content-security-policy'), None)
    print("serving %s" % os.path.normpath(SITE))
    print("  http://localhost:%d/" % port)
    print("  cleanUrls=%s  headers replayed=%d" % (CLEAN_URLS, len(HEADERS)))
    print("  CSP: %s" % ((csp[:96] + '...') if csp else 'NONE FOUND — check vercel.json'))
    sys.stdout.flush()
    srv.serve_forever()


if __name__ == '__main__':
    main()
