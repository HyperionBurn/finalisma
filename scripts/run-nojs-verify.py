"""Drive the no-JS cohort verification: in-process site server + node subprocess.

Per workflow.md: a bash tool call does not return until every descendant
holding the pipe has exited, so a background server blocks forever. This driver
starts the site server IN-PROCESS (daemon thread), shells out to the node
harness with a subprocess (no pipe), and tears the server down in ``finally``.

Usage:
    python -B scripts/run-nojs-verify.py

Exit code mirrors the node harness (0 = the cohort widget is inert without JS).
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
