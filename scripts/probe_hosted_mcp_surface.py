"""Read-only authenticated probe for the hosted Weft MCP tool catalog.

The probe performs only initialize and tools/list against POST /mcp. It uses a
release-scoped bearer token from an environment variable and never creates,
closes, joins, sends, or waits in a Room.

Run:
    $env:WEFT_MCP_ORIGIN = "https://YOUR-VERIFIED-API-ORIGIN"
    $env:WEFT_MCP_PROBE_TOKEN = "<release-scoped-token>"
    python -B scripts/probe_hosted_mcp_surface.py --pretty

The token is read from the environment so it is not placed in command-line
arguments or probe output.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MCP_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {MCP_PROTOCOL_VERSION, "2024-11-05"}
DEFAULT_TOKEN_ENV = "WEFT_MCP_PROBE_TOKEN"
MAX_RESPONSE_BYTES = 1024 * 1024
EXPECTED_TOOL_NAMES = (
    "room_create",
    "room_list",
    "room_join",
    "room_send",
    "room_receipts",
    "room_poll",
    "room_wait",
    "room_info",
    "room_ack",
    "room_heartbeat",
    "room_leave",
    "room_remove_member",
    "room_close",
    "room_event_log",
)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_OPENER = build_opener(_NoRedirectHandler)


def _origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"mcp origin must be an absolute http(s) URL: {value!r}")
    if parsed.username or parsed.password:
        raise ValueError("mcp origin must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("mcp origin must not contain a query string or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("mcp origin must contain only a URL origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def _facts(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": response.get("status"),
        "content_type": response.get("content_type", ""),
        "bytes": response.get("bytes", 0),
        "error": response.get("error"),
    }


def _read_bounded(response: Any) -> tuple[bytes, int, str | None]:
    declared = response.headers.get("Content-Length")
    try:
        declared_size = int(declared) if declared is not None else None
    except ValueError:
        declared_size = None
    if declared_size is not None and declared_size > MAX_RESPONSE_BYTES:
        return b"", declared_size, "response_too_large"
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        return b"", len(body), "response_too_large"
    return body, len(body), None


def _post_json(
    endpoint: str,
    token: str,
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    request = Request(
        endpoint,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "weft-hosted-mcp-surface-probe/1",
        },
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body, byte_count, read_error = _read_bounded(response)
            return {
                "status": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "bytes": byte_count,
                "body": body,
                "error": read_error,
            }
    except HTTPError as exc:
        try:
            body, byte_count, read_error = _read_bounded(exc)
        except OSError:
            body = b""
            byte_count = 0
            read_error = "http_error"
        exc.close()
        return {
            "status": exc.code,
            "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
            "bytes": byte_count,
            "body": body,
            "error": read_error or "http_error",
        }
    except (OSError, URLError, TimeoutError):
        return {
            "status": None,
            "content_type": "",
            "bytes": 0,
            "body": b"",
            "error": "transport_error",
        }


def _rpc(
    endpoint: str,
    token: str,
    request_id: int,
    method: str,
    params: dict[str, Any] | None,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    response = _post_json(endpoint, token, payload, timeout)
    safe = _facts(response)
    if response["status"] != 200:
        return safe, None
    if response["error"]:
        return safe, None
    content_type = response["content_type"].split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        body = response["body"]
    elif content_type == "text/event-stream":
        body = b""
        for line in response["body"].splitlines():
            if line.startswith(b"data:"):
                body = line[5:].strip()
                break
        if not body:
            safe["error"] = "invalid_sse"
            return safe, None
    else:
        safe["error"] = "invalid_content_type"
        return safe, None
    try:
        reply = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        safe["error"] = "invalid_json"
        return safe, None
    if (
        not isinstance(reply, dict)
        or reply.get("jsonrpc") != "2.0"
        or reply.get("id") != request_id
    ):
        safe["error"] = "invalid_jsonrpc"
        return safe, None
    if "error" in reply:
        error = reply.get("error")
        safe["error"] = (
            f"jsonrpc_error:{error.get('code')}"
            if isinstance(error, dict) and "code" in error
            else "jsonrpc_error"
        )
        return safe, None
    result = reply.get("result")
    if not isinstance(result, dict):
        safe["error"] = "invalid_result"
        return safe, None
    return safe, result


def _notify(
    endpoint: str,
    token: str,
    method: str,
    timeout: float,
) -> tuple[dict[str, Any], bool]:
    response = _post_json(
        endpoint,
        token,
        {"jsonrpc": "2.0", "method": method},
        timeout,
    )
    safe = _facts(response)
    return safe, response["status"] == 202


def probe(mcp_origin: str, token: str, timeout: float = 10.0) -> dict[str, Any]:
    origin = _origin(mcp_origin)
    endpoint = f"{origin}/mcp"
    requests = ["initialize"]
    init_facts, init_result = _rpc(
        endpoint,
        token,
        1,
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": "weft-hosted-mcp-surface-probe",
                "version": "1.0.0",
            },
        },
        timeout,
    )
    server_info = init_result.get("serverInfo") if init_result else None
    init_ok = (
        init_facts["status"] == 200
        and isinstance(init_result, dict)
        and init_result.get("protocolVersion") in SUPPORTED_PROTOCOL_VERSIONS
        and isinstance(server_info, dict)
        and isinstance(server_info.get("name"), str)
        and bool(server_info.get("name"))
        and isinstance(server_info.get("version"), str)
        and bool(server_info.get("version"))
    )
    initialized_facts: dict[str, Any] = {
        "status": None,
        "content_type": "",
        "bytes": 0,
        "error": "not_attempted",
    }
    initialized_ok = False
    tools_facts: dict[str, Any] = {
        "status": None,
        "content_type": "",
        "bytes": 0,
        "error": "not_attempted",
    }
    tools_result: dict[str, Any] | None = None
    if init_ok:
        requests.append("notifications/initialized")
        initialized_facts, initialized_ok = _notify(
            endpoint,
            token,
            "notifications/initialized",
            timeout,
        )
    if init_ok and initialized_ok:
        requests.append("tools/list")
        tools_facts, tools_result = _rpc(
            endpoint,
            token,
            2,
            "tools/list",
            None,
            timeout,
        )

    tool_names: list[str] = []
    if tools_result is not None:
        raw_tools = tools_result.get("tools")
        if isinstance(raw_tools, list) and all(isinstance(tool, dict) for tool in raw_tools):
            tool_names = [
                tool["name"]
                for tool in raw_tools
                if isinstance(tool.get("name"), str)
            ]
    missing_tools = sorted(set(EXPECTED_TOOL_NAMES) - set(tool_names))
    unexpected_tools = sorted(set(tool_names) - set(EXPECTED_TOOL_NAMES))
    duplicate_tools = len(tool_names) != len(set(tool_names))
    catalog_ok = (
        tools_facts["status"] == 200
        and not duplicate_tools
        and len(tool_names) == len(EXPECTED_TOOL_NAMES)
        and not missing_tools
        and not unexpected_tools
    )
    attempted_facts = [init_facts, initialized_facts, tools_facts]
    reachable = init_facts["status"] is not None
    credential_rejected = any(
        facts["status"] in {401, 403} for facts in attempted_facts
    )
    authenticated = init_facts["status"] == 200 and not credential_rejected
    checks = {
        "authenticated": authenticated,
        "initialize": init_ok,
        "initialized_notification": initialized_ok,
        "tools_catalog": catalog_ok,
        "read_only_rpc": True,
    }
    if all(checks.values()):
        status = "PASS"
    elif credential_rejected:
        status = "UNAUTHORIZED"
    elif not reachable:
        status = "UNREACHABLE"
    else:
        status = "DRIFT"
    diagnostics: list[dict[str, Any]] = []
    if not authenticated:
        diagnostics.append({
            "check": "authenticated",
            "expected": "the release-scoped token must be accepted by POST /mcp",
            "initialize": _facts(init_facts),
            "initialized": _facts(initialized_facts),
            "tools_list": _facts(tools_facts),
        })
    if not init_ok:
        diagnostics.append({
            "check": "initialize",
            "expected": "HTTP 200 JSON-RPC result with protocolVersion and serverInfo",
            "endpoint": _facts(init_facts),
        })
    if not initialized_ok:
        diagnostics.append({
            "check": "initialized_notification",
            "expected": "HTTP 202 after a successful initialize response",
            "endpoint": _facts(initialized_facts),
        })
    if not catalog_ok:
        diagnostics.append({
            "check": "tools_catalog",
            "expected_count": len(EXPECTED_TOOL_NAMES),
            "actual_count": len(tool_names),
            "missing_tools": missing_tools,
            "unexpected_tools": unexpected_tools,
            "duplicate_tools": duplicate_tools,
            "endpoint": _facts(tools_facts),
        })
    return {
        "probe": "weft-hosted-mcp-surface-v1",
        "origin": origin,
        "status": status,
        "checks": checks,
        "tool_count": len(tool_names),
        "diagnostics": diagnostics,
        "requests": requests,
        "endpoints": {
            "initialize": _facts(init_facts),
            "initialized": _facts(initialized_facts),
            "tools_list": _facts(tools_facts),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mcp-origin",
        default=None,
        help="verified API origin, or set WEFT_MCP_ORIGIN",
    )
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help=f"environment variable containing the token (default: {DEFAULT_TOKEN_ENV})",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    origin = (args.mcp_origin or os.environ.get("WEFT_MCP_ORIGIN", "")).strip()
    token = os.environ.get(args.token_env, "")
    missing = []
    if not origin:
        missing.append("--mcp-origin or WEFT_MCP_ORIGIN")
    if not token:
        missing.append(f"the {args.token_env} environment variable")
    if missing:
        result = {
            "probe": "weft-hosted-mcp-surface-v1",
            "status": "INVALID_ARGUMENT",
            "error": "Explicit origin and token are required: " + ", ".join(missing) + ".",
        }
        print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
        return 4
    try:
        result = probe(origin, token, timeout=args.timeout)
    except ValueError as exc:
        result = {
            "probe": "weft-hosted-mcp-surface-v1",
            "status": "INVALID_ARGUMENT",
            "error": str(exc),
        }
        print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
        return 4
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return {
        "PASS": 0,
        "DRIFT": 2,
        "UNAUTHORIZED": 5,
        "UNREACHABLE": 3,
    }[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
