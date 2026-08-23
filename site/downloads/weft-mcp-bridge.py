#!/usr/bin/env python3
# GENERATED FILE — do not hand-edit.
#
# Source of truth: src/weft_mcp/stdio_bridge.py (the bridge logic, unchanged
# below) plus the CLI front door appended at the end of this file. Regenerate
# with `python scripts/build_weft_mcp_bridge.py` from a checkout of
# https://github.com/HyperionBurn/finalisma — running THIS file does not
# require that checkout; it requires only a Python 3.11+ standard library.
"""Weft MCP bridge — standalone, no-install download.

This ONE file is the entire program. It has no dependencies beyond the
Python standard library and no sibling files to go with it — download it,
point your MCP host at it, and it runs. You do not need the weft-mcp
package (not published to PyPI), and you do not need the source
repository (private).

What it does: bridges a stdio MCP client (Claude Desktop, Cursor, Codex,
Claude Code — anything that launches an MCP server as a `command` + `args`
subprocess) to the HOSTED Weft service's Streamable-HTTP `POST /mcp`
endpoint. Every JSON-RPC message your MCP host writes to this process's
stdin is forwarded to `<remote>/mcp` with `Authorization: Bearer <token>`;
the reply comes back on stdout. Your host sees the hosted room tools as if
they were a local process.

Usage::

    python weft-mcp-bridge.py --remote https://<your-weft-origin> --token-env WEFT_TOKEN

- `--remote <origin>` — the hosted service's base URL. Required: this file
  only speaks to a hosted endpoint: it has no local coordinator mode.
- `--token-env <NAME>` — the name of an environment variable holding your
  bearer token (an `agk_` agent key, minted from the dashboard's Connector
  config page, or an `fss_` session token). Defaults to `WEFT_TOKEN`. The
  token is read from that environment variable ONLY, never from a
  command-line argument — argv is visible to every process on the machine.

Get an updated copy any time by re-downloading from the same URL you got
this one from; the file is small and safe to replace outright.

Full behavior, failure-mode discipline, and the wire contract are documented
below, inline, in the forwarding code this file wraps.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import sys
from typing import Any, TextIO
from urllib.parse import urlsplit

MAX_JSON_RPC_BYTES = 512 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
_SAFE_RETRY_AFTER_MAX = 86400


def _reject_json_constant(value: str) -> None:
    """Reject JavaScript-style numeric constants outside JSON RFC 8259."""
    raise ValueError(f"non-standard JSON constant: {value}")


# ---------------------------------------------------------------------------
# Response unwrapping
# ---------------------------------------------------------------------------


def unwrap_response_body(body: bytes, content_type: str) -> dict[str, Any] | None:
    """Unwrap a hosted MCP HTTP response body into a JSON-RPC envelope.

    Accepts plain JSON (``application/json``) or an SSE-framed body
    (``text/event-stream``: ``event:``/``data:`` lines). Returns ``None`` for
    an empty body (e.g. a 202 notification acknowledgement).
    """
    if not body:
        return None
    text = body.decode("utf-8", errors="replace")
    ctype = (content_type or "").lower()
    if "text/event-stream" in ctype or "data:" in text:
        data_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data:"):
                data_lines.append(stripped[len("data:"):].lstrip())
        if not data_lines:
            return None
        text = "\n".join(data_lines)
    return json.loads(text, parse_constant=_reject_json_constant)


def _json_rpc_error(
    request_id: Any,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _safe_retry_after(value: Any) -> int | None:
    """Keep only a bounded numeric retry hint from an upstream response."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or value > _SAFE_RETRY_AFTER_MAX:
        return None
    return max(1, int(value))


def _header_retry_after(value: str | None) -> int | None:
    """Parse an HTTP Retry-After delta without copying arbitrary header text."""
    if not isinstance(value, str) or not value.strip().isdigit():
        return None
    return _safe_retry_after(int(value.strip()))


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


class StdioHttpBridge:
    """Forwards stdio JSON-RPC lines to a hosted Weft ``/mcp`` endpoint."""

    def __init__(self, remote: str, token_env: str):
        if not remote:
            raise ValueError("--remote requires a URL like https://weft.example.com")
        parts = urlsplit(remote if "://" in remote else "https://" + remote)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"invalid --remote URL: {remote!r}")
        if parts.scheme == "http" and parts.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "--remote must use https:// for non-loopback hosts; "
                "cleartext HTTP is allowed only for local testing"
            )
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        base_path = (parts.path or "/").rstrip("/")
        if base_path.endswith("/mcp"):
            self.path = base_path
        else:
            self.path = base_path + "/mcp"
        self.origin = f"{self.scheme}://{parts.netloc}"
        self.token_env = token_env
        self.session_id: str | None = None
        self._last_status: int | None = None

    def _token(self) -> str | None:
        return os.environ.get(self.token_env)

    def has_token(self) -> bool:
        return bool(os.environ.get(self.token_env))

    def _connect(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port,
                                               timeout=DEFAULT_TIMEOUT_SECONDS)
        return http.client.HTTPConnection(self.host, self.port,
                                          timeout=DEFAULT_TIMEOUT_SECONDS)

    def _exchange_once(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Send one JSON-RPC message to the hosted endpoint and return the reply.

        Returns ``None`` for a notification acknowledgement (empty 202 body),
        which the caller must translate into NO reply line on stdout.
        """
        token = self._token()
        if not token:
            return _json_rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32000,
                f"no Weft bearer token found: environment variable {self.token_env} is not set or empty",
            )

        payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        conn = self._connect()
        try:
            try:
                conn.request("POST", self.path, body=payload, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                body = resp.read()
                content_type = resp.getheader("Content-Type") or "application/json"
                retry_after = resp.getheader("Retry-After")
                session_id = resp.getheader("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
            finally:
                conn.close()
        except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
            self._last_status = None
            return _json_rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32000,
                f"could not reach the Weft hosted MCP endpoint at {self.origin} "
                f"({type(exc).__name__}: {exc})",
            )

        self._last_status = status
        if status == 401:
            # A long-running agent has no human watching to notice a logout.
            # The message must say BOTH what happened AND how to recover, or the
            # agent just sees a dead channel mid-conversation. It names the
            # recovery step (a fresh signin) and the exact request shape, but
            # never the old token or any credentials.
            return _json_rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32000,
                "authentication failed: the Weft bearer token is invalid or revoked "
                "(the hosted endpoint returned 401). An fss_ session stops working "
                "if the service was redeployed, the account's password was reset, or "
                "the session expired; an agk_ agent key can be revoked or invalidated "
                "by a password reset or membership removal. Re-authenticate with "
                "POST /v1/auth/signin and {\"email\": ..., \"password\": ...} for "
                "a fresh session_token (prefix fss_), or create a replacement key "
                "with POST /v1/agent-keys; export the new credential in the "
                "environment variable used for this bridge (the --token-env name) "
                "and restart this client",
            )
        if status == 429:
            retry_hint: int | None = _header_retry_after(retry_after)
            try:
                decoded = json.loads(body.decode("utf-8")) if body else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = None
            if retry_hint is None and isinstance(decoded, dict) and isinstance(decoded.get("error"), dict):
                error = decoded["error"]
                retry_hint = _safe_retry_after(error.get("retry_after"))
                if retry_hint is None and isinstance(error.get("details"), dict):
                    retry_hint = _safe_retry_after(error["details"].get("retry_after"))
            safe_data: dict[str, Any] = {"code": "rate_limited"}
            if retry_hint is not None:
                safe_data["retry_after"] = retry_hint
            return _json_rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32000,
                "the Weft hosted endpoint returned HTTP 429; retry later",
                safe_data,
            )
        if status == 202:
            return None
        if status != 200:
            return _json_rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32000,
                f"the Weft hosted endpoint at {self.origin} returned HTTP {status}",
            )
        try:
            return unwrap_response_body(body, content_type)
        except json.JSONDecodeError:
            return _json_rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32603,
                "the Weft hosted endpoint returned an unparseable response",
            )

    def exchange(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Exchange one message and recover once from a stale HTTP session.

        Streamable HTTP servers may invalidate ``Mcp-Session-Id`` after a
        restart. Retrying the original call without first initializing would
        still fail, while blindly retrying a tool call could duplicate work.
        Clear the stale ID, initialize exactly once, then retry the original
        request once with the fresh ID.
        """
        had_session = self.session_id is not None
        response = self._exchange_once(request)
        if not had_session or self._last_status not in (400, 404):
            return response

        self.session_id = None
        initialize = {
            "jsonrpc": "2.0",
            "id": "weft-stdio-reinitialize",
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
        }
        initialized = self._exchange_once(initialize)
        if (
            not isinstance(initialized, dict)
            or "error" in initialized
            or self._last_status != 200
            or self.session_id is None
        ):
            return _json_rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32000,
                "the Weft hosted MCP session was stale and could not be reinitialized; restart the client",
            )
        return self._exchange_once(request)


def run_stdio_bridge(
    bridge: StdioHttpBridge,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    """Read JSON-RPC lines from stdin and relay them to the hosted endpoint.

    Mirrors ``weft_mcp.server.run_stdio`` framing: one JSON-RPC object per
    line in, one per line out; notifications produce no reply line.

    Returns 0 on a clean EOF. Returns non-zero when the bridge is
    misconfigured (missing token) so the process FAILS LOUDLY: the client sees
    a renderable JSON-RPC error AND a non-zero exit, never a silent hang.
    """
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    # The MCP spec requires JSON-RPC over UTF-8 in BOTH directions. On Windows
    # Python defaults stdin/stdout to the ANSI codepage (cp1252), which corrupts
    # every non-ASCII character a client sends. The outbound side was fixed
    # before; reconfigure the INPUT stream too so inbound bytes are decoded as
    # UTF-8. errors="strict": a frame that is not valid UTF-8 is a JSON-RPC
    # protocol violation and must fail loudly, not be silently replaced with
    # U+FFFD and re-corrupted without a signal.
    try:
        input_stream.reconfigure(encoding="utf-8", errors="strict")
    except (AttributeError, ValueError, OSError):
        # An injected stream that does not support reconfigure (e.g. StringIO)
        # is left untouched; it has no codepage to corrupt.
        pass
    try:
        output_stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        # An injected stream that does not support reconfigure (e.g. StringIO)
        # is left untouched; it has no codepage to corrupt.
        pass
    config_failed = not bridge.has_token()
    for raw_line in input_stream:
        if len(raw_line.encode("utf-8", errors="ignore")) > MAX_JSON_RPC_BYTES:
            response = _json_rpc_error(None, -32600, "JSON-RPC message exceeds the size limit")
        else:
            try:
                request = json.loads(raw_line, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError):
                response = _json_rpc_error(None, -32700, "Parse error")
            else:
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    response = _json_rpc_error(
                        request.get("id") if isinstance(request, dict) else None,
                        -32600, "Invalid JSON-RPC request")
                else:
                    is_notification = "id" not in request
                    response = bridge.exchange(request)
                    if is_notification:
                        # A notification never gets a reply line, even if the
                        # upstream returned a body for it.
                        continue
                    if config_failed:
                        # The token was missing; we surfaced a JSON-RPC error
                        # naming the env var. Stop so the process exits non-zero.
                        if response is not None:
                            output_stream.write(json.dumps(response, ensure_ascii=False,
                                                           separators=(",", ":")) + "\n")
                            output_stream.flush()
                        return 1
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_stream.flush()
    return 1 if config_failed else 0


# ---------------------------------------------------------------------------
# CLI entry point. Nothing above this line imports anything outside the
# standard library, and nothing above it needs any other file — that is the
# whole point of shipping this as one download.
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weft-mcp-bridge.py",
        description=(
            "Bridge a stdio MCP client to the hosted Weft service's "
            "POST /mcp endpoint."
        ),
    )
    parser.add_argument(
        "--remote",
        required=True,
        help=(
            "Hosted Weft origin, e.g. https://weft.example.com "
            "(the bridge POSTs to <remote>/mcp)"
        ),
    )
    parser.add_argument(
        "--token-env",
        default="WEFT_TOKEN",
        help=(
            "Environment variable holding the bearer token (default: "
            "WEFT_TOKEN). Never pass the token itself on the command line."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        bridge = StdioHttpBridge(args.remote, args.token_env)
    except ValueError as exc:
        raise SystemExit(f"weft-mcp-bridge: {exc}") from exc
    return run_stdio_bridge(bridge)


if __name__ == "__main__":
    raise SystemExit(main())
