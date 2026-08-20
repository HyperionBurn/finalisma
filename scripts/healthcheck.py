#!/usr/bin/env python3
"""Dependency-free outage probe: know about it before the owner does.

Hits `GET /healthz` and an unauthenticated `POST /mcp` (which must return
401 — anything else means the hosted MCP surface is either down or, worse,
silently falling through to the web app's login redirect, which is exactly
the bug that once made the hosted service unreachable from MCP clients).
Appends one JSON line per run to a local log, and exits non-zero on any
failure so a systemd timer's failure state (`systemctl --failed`) makes the
outage visible without anyone needing to notice it first.

Deliberately dependency-free (stdlib `urllib.request` only, no `requests`)
and deliberately local — no sign-up, no third-party monitoring service, no
outbound call other than to the target host itself.

Never logs response bodies or headers (could carry data), never logs
credentials (none are ever sent — the whole point is to probe unauthenticated).
Only status codes, timing, and error class/message are recorded.

Usage::

    python3 healthcheck.py --base-url http://127.0.0.1:18788 \\
        --edge-url https://rooms.example.test \\
        --log /var/log/weft/healthcheck.jsonl

Exit 0: both probes behaved as expected.
Exit 1: at least one probe failed (VM's owner should see this via
`systemctl --failed` / the log file already reflects why).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the status code visible when probing for login fallthrough."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class ProbeResult:
    ok: bool
    status: "int | None"
    latency_ms: float
    detail: str


def probe_healthz(base_url: str, *, timeout: float = 5.0) -> ProbeResult:
    url = base_url.rstrip("/") + "/healthz"
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        # HTTPError is itself a file-like response object (it carries the
        # socket for the error body) — close it explicitly, or it leaks
        # until the garbage collector gets to it.
        status = exc.code
        exc.close()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        latency_ms = (time.monotonic() - started) * 1000
        return ProbeResult(False, None, round(latency_ms, 1), f"unreachable: {exc}")

    latency_ms = (time.monotonic() - started) * 1000
    if status == 200:
        return ProbeResult(True, status, round(latency_ms, 1), "ok")
    return ProbeResult(False, status, round(latency_ms, 1), f"expected 200, got {status}")


def probe_unauth_mcp(base_url: str, *, timeout: float = 5.0) -> ProbeResult:
    """POST /mcp with no auth header. Must be 401.

    303/302 means the request fell through nginx's catch-all into the web
    app's login redirect instead of reaching the MCP surface at all — the
    exact failure mode that once made the hosted service unreachable from
    Claude Code / Cursor / Zed. Anything else is unexpected and treated as a
    failure so it gets investigated rather than assumed fine.
    """
    url = base_url.rstrip("/") + "/mcp"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    started = time.monotonic()
    try:
        # A 302/303 is the signal we are looking for.  Following it would
        # hide the routing bug behind the web app's eventual response.
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        # HTTPError is itself a file-like response object (it carries the
        # socket for the error body) — close it explicitly, or it leaks
        # until the garbage collector gets to it.
        status = exc.code
        exc.close()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        latency_ms = (time.monotonic() - started) * 1000
        return ProbeResult(False, None, round(latency_ms, 1), f"unreachable: {exc}")

    latency_ms = (time.monotonic() - started) * 1000
    if status == 401:
        return ProbeResult(True, status, round(latency_ms, 1), "correctly refused, reached the MCP surface")
    if status in (302, 303):
        return ProbeResult(
            False, status, round(latency_ms, 1),
            "falling through to the web app login redirect, not reaching MCP",
        )
    return ProbeResult(False, status, round(latency_ms, 1), f"unexpected status {status}")


def run(base_url: str, *, edge_url: str | None = None,
        timeout: float = 5.0) -> "tuple[bool, dict]":
    """Probe the backend health and the public edge independently.

    ``edge_url`` defaults to ``base_url`` for existing callers, but a hosted
    installation should point it at nginx/public routing so a catch-all login
    redirect cannot make the timer report a false green.
    """
    edge_url = edge_url or base_url
    healthz = probe_healthz(base_url, timeout=timeout)
    mcp = probe_unauth_mcp(edge_url, timeout=timeout)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": base_url,
        "edge_url": edge_url,
        "healthz": asdict(healthz),
        "mcp_unauth": asdict(mcp),
    }
    return (healthz.ok and mcp.ok), record


def append_log(log_path: Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _main(argv: "list[str]") -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True,
                        help="backend URL for /healthz, e.g. http://127.0.0.1:18788")
    parser.add_argument("--edge-url", default=None,
                        help="nginx/public URL for /mcp; defaults to --base-url")
    parser.add_argument(
        "--require-https-edge",
        action="store_true",
        help="fail before probing unless the edge URL uses HTTPS",
    )
    parser.add_argument("--log", type=Path, default=None, help="append a JSON line here")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    edge_url = args.edge_url or args.base_url
    if args.require_https_edge:
        parsed = urlsplit(edge_url)
        if parsed.scheme != "https" or not parsed.netloc:
            parser.error("--edge-url must be an absolute HTTPS URL when --require-https-edge is set")

    ok, record = run(args.base_url, edge_url=args.edge_url, timeout=args.timeout)

    if args.log is not None:
        append_log(args.log, record)

    print(json.dumps(record, indent=2, sort_keys=True))
    if not ok:
        print("HEALTHCHECK FAILED", file=sys.stderr)
        return 1
    print("healthcheck ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
