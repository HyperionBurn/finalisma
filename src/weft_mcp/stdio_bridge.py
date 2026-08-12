"""stdio <-> Streamable-HTTP MCP bridge for the hosted Weft service.

Real MCP hosts (Claude Desktop, Cursor, Claude Code, Codex) launch MCP servers
as a subprocess: ``command`` + ``args`` in a config file, speaking JSON-RPC
over stdin/stdout. They have no ``url`` form, so today they can only reach the
LOCAL coordinator — a separate database with its own rooms. This module is the
shim that lets any stdio MCP client reach the HOSTED ``/mcp`` endpoint:

    weft-mcp --remote https://<origin> --token-env WEFT_TOKEN

Behaviour, wire for wire:

  * Every JSON-RPC message read from stdin is forwarded unchanged to
    ``POST <origin>/mcp`` with ``Authorization: Bearer <token>``. The tool set
    is never filtered or rewritten — whatever the hosted endpoint exposes is
    what the client sees.
  * Responses are unwrapped from either Streamable-HTTP shape the server may
    choose: plain JSON (``application/json``) or an SSE-framed body
    (``text/event-stream`` with ``event:`` / ``data:`` lines). Getting the
    unwrap wrong looks like a silent hang to the client, so both shapes are
    handled explicitly and covered by tests.
  * A server-issued ``Mcp-Session-Id`` response header is preserved across the
    session and echoed on every subsequent request.
  * Notifications (requests without ``id``) produce no reply line on stdout,
    matching the JSON-RPC/MCP convention the local coordinator already follows.

Failure mode discipline — stdio hosts swallow stderr, so misconfiguration must
surface as a JSON-RPC error the client will DISPLAY, never a silent exit:

  * no token in the configured env var  -> JSON-RPC error naming the variable
  * upstream 401                        -> JSON-RPC error saying the token is
    invalid or expired
  * connection refused / DNS failure    -> JSON-RPC error naming the origin it
    tried

The token is read from an environment variable only — never from argv, which is
visible to every process on the machine. Token prefixes (``rm_``, ``fst_actor_``,
``fss_``) and the ``weft.a2a`` namespace are not touched by this module.

Authoritative spec: docs/STDIO_BRIDGE.md.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
from typing import Any, TextIO
from urllib.parse import urlsplit

MAX_JSON_RPC_BYTES = 512 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0


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
    return json.loads(text)


def _json_rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


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

    def exchange(self, request: dict[str, Any]) -> dict[str, Any] | None:
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
                session_id = resp.getheader("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
            finally:
                conn.close()
        except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
            return _json_rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32000,
                f"could not reach the Weft hosted MCP endpoint at {self.origin} "
                f"({type(exc).__name__}: {exc})",
            )

        if status == 401:
            # A long-running agent has no human watching to notice a logout.
            # The message must say BOTH what happened AND how to recover, or the
            # agent just sees a dead channel mid-conversation. It names the
            # recovery step (a fresh signin) and the exact request shape, but
            # never the old token or any credentials.
            return _json_rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32000,
                "authentication failed: the Weft session token is invalid or expired "
                "(the hosted endpoint returned 401). The session token stops working "
                "if the service was redeployed, the account's password was reset, or "
                "the token expired. Re-authenticate: POST /v1/auth/signin with "
                "{\"email\": ..., \"password\": ...} returns a fresh session_token "
                "(prefix fss_); export it in the environment variable used for this "
                "bridge (the --token-env name) and restart this client",
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
                request = json.loads(raw_line)
            except json.JSONDecodeError:
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
