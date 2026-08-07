"""Weft Web App — runnable entry point.

``python -m weft_cloud.web`` binds the browser front-end (the same
``WeftWebApp`` the 93 web-contract tests drive) to a real HTTP port so the
marketing site's signup CTA has something to point at. Without this module the
web app is green but undeployable: it is the last thing between "works" and
"deployable".

Architecture (per docs/DEPLOY.md):
  - ``src/weft_mcp/`` stays stdlib-only forever — UNTOUCHED by this module.
  - ``src/weft_cloud/`` MAY take dependencies, but this entry point stays
    stdlib-only to keep the dependency-free promise intact for v1.
  - Transport is the same idiom as ``weft_mcp/server.py`` and
    ``weft_cloud/service.py``: ``http.server.ThreadingHTTPServer`` +
    ``BaseHTTPRequestHandler`` (``app.handler``).

The launcher sequence is deliberately the one verified by hand before this
module existed: initialize the SQLite-WAL backend, ensure identity schema, then
construct ``WeftWebApp(backend, static_dir=..., state_dir=...)`` and serve
``app.handler``. Do not reorder it casually — it is the regression contract
``tests/test_webapp_entrypoint.py`` encodes.
"""

from __future__ import annotations

import os
import sys
import http.server
from typing import Any, Mapping

from weft_cloud.identity.schema import ensure_schema
from weft_cloud.storage import SqliteWalBackend
from weft_cloud.web.app import WeftWebApp

# Secrets are never logged. No secret ever appears in a default configuration.


def runtime_config(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve runtime settings for the web app launcher.

    Same precedence and discipline as ``weft_cloud.service.runtime_config``
    (argv → environment → defaults), so containers configure entirely through
    the environment while a developer can still pass a positional port:

    - ``WEFT_WEB_HOST``        (default ``127.0.0.1``)
    - ``WEFT_WEB_PORT``        (default ``18789``)
    - ``WEFT_WEB_DB_PATH``     (default ``./data/weft-web.db``)
    - ``WEFT_WEB_STATIC_DIR``  (default ``./site``)
    - ``WEFT_WEB_STATE_DIR``   (default ``./data``)

    A malformed or out-of-range port raises ``ValueError`` so a misconfigured
    deploy fails loudly at startup instead of silently binding a default.
    """
    argv = list(argv) if argv is not None else []
    environ = os.environ if environ is None else environ

    def _port(name: str, raw: str | None, default: int) -> int:
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer, got {raw!r}")
        if not (1 <= value <= 65535):
            raise ValueError(f"{name} must be in 1..65535, got {value}")
        return value

    host = environ.get("WEFT_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = _port("WEFT_WEB_PORT", environ.get("WEFT_WEB_PORT"), 18789)
    db_path = environ.get("WEFT_WEB_DB_PATH", "./data/weft-web.db").strip() \
        or "./data/weft-web.db"
    static_dir = environ.get("WEFT_WEB_STATIC_DIR", "./site").strip() or "./site"
    state_dir = environ.get("WEFT_WEB_STATE_DIR", "./data").strip() or "./data"

    if len(argv) >= 1:
        port = _port("port", argv[0], port)
    if len(argv) >= 2:
        db_path = argv[1]

    return {
        "host": host,
        "port": port,
        "db_path": db_path,
        "static_dir": static_dir,
        "state_dir": state_dir,
    }


def serve(
    host: str = "127.0.0.1",
    port: int = 18789,
    db_path: str = "./data/weft-web.db",
    static_dir: str = "./site",
    state_dir: str = "./data",
) -> None:
    """Run the web app HTTP service (blocking)."""
    backend = SqliteWalBackend(db_path)
    backend.initialize()
    ensure_schema(backend)
    app = WeftWebApp(backend, static_dir=static_dir, state_dir=state_dir)
    server = http.server.ThreadingHTTPServer((host, port), app.handler)
    # Report the ACTUAL bound address so port=0 (test) prints a usable URL.
    actual_host, actual_port = server.server_address
    print(f"weft-web listening on http://{actual_host}:{actual_port}",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        backend.close()


if __name__ == "__main__":
    try:
        cfg = runtime_config(sys.argv[1:])
    except ValueError as exc:
        print(f"weft-web: {exc}", file=sys.stderr)
        sys.exit(2)
    serve(cfg["host"], cfg["port"], cfg["db_path"], cfg["static_dir"], cfg["state_dir"])
