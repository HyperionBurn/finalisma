"""Wave D3 QA driver — owns an in-process site server and runs the Playwright harness.

Per workflow.md §7.1: a bash tool call does not return until every descendant
holding the pipe has exited, so a background server blocks forever. This driver
starts the site server IN-PROCESS (daemon thread), shells out to the node
harness with a subprocess (no pipe), and tears the server down in ``finally``.

Usage:
    python -B scripts/run-site-qa.py

Exit code mirrors the node harness (0 = all checks pass).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietSiteHandler, directory=str(ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/"
        env = dict(os.environ)
        env["WEFT_SITE_URL"] = url
        result = subprocess.run(
            ["node", str(ROOT / "scripts" / "capture-site-qa.cjs")],
            env=env,
            cwd=str(ROOT),
        )
        return result.returncode
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
