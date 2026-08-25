"""Shared readiness checks for in-process HTTP test servers."""

from __future__ import annotations

import time
import urllib.error
import urllib.request


def await_serving(server_or_port: object, *, deadline: float = 10.0) -> None:
    """Block until an in-process HTTP server actually answers a request.

    ``HTTPServer.__init__`` binds and listens before the thread running
    ``serve_forever`` is scheduled. A connect-only probe therefore reports
    readiness too early. This sends a real request and accepts any HTTP
    response, including an error response, as proof that the handler thread is
    live.
    """
    if isinstance(server_or_port, int):
        port = server_or_port
    else:
        port = int(getattr(server_or_port, "server_address")[1])

    end = time.monotonic() + deadline
    last = "no attempt made"
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=1.0) as response:
                response.read()
            return
        except urllib.error.HTTPError:
            return
        except Exception as exc:  # not listening yet, or not yet answering
            last = repr(exc)
            time.sleep(0.02)
    raise AssertionError(
        f"server on port {port} never answered within {deadline}s: {last}")
