"""Measure the PRE-FIX cohort widget with JS disabled (before evidence).

Serves the git-HEAD built site/index.html (which still had the bare <form>
with type="submit" and no action/method) and runs verify-nojs-cohort.cjs
against it, proving the leak existed before the fix.

Usage:
    python -B scripts/run-nojs-verify-before.py

Read-only: the old page is materialised under the OS temp dir, never in the
repo, and the repo's site/ is untouched.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_site_spec = importlib.util.spec_from_file_location(
    "weft_site", ROOT / "scripts" / "weft-site.py"
)
assert _site_spec and _site_spec.loader
_site_module = importlib.util.module_from_spec(_site_spec)
_site_spec.loader.exec_module(_site_module)
QuietSiteHandler = _site_module.QuietSiteHandler


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="weft-nojs-before-") as temporary:
        site_dir = Path(temporary) / "site"
        site_dir.mkdir()
        old_html = subprocess.run(
            ["git", "-C", str(ROOT), "show", "HEAD:site/index.html"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        (site_dir / "index.html").write_text(old_html, encoding="utf-8")
        # QuietSiteHandler serves a branded 404 on missing paths; the old page
        # references assets the temp dir does not carry, so a 404 page must
        # exist for those requests to resolve.
        (site_dir / "404.html").write_text(
            "<!DOCTYPE html><html><body>not found</body></html>",
            encoding="utf-8",
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietSiteHandler, directory=temporary))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            url = f"http://{host}:{port}/"
            env = dict(os.environ)
            env["WEFT_SITE_URL"] = url
            result = subprocess.run(
                ["node", str(ROOT / "scripts" / "verify-nojs-cohort.cjs")],
                env=env,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            if result.stdout:
                print(result.stdout.strip())
            if result.stderr:
                print(result.stderr.strip(), file=sys.stderr)
            return result.returncode
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
