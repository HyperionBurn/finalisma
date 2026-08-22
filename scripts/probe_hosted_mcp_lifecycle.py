"""Authenticated lifecycle probe for the hosted Weft MCP room surface.

The read-only catalog probe intentionally never mutates a Room. This separate
probe uses a dedicated owner/admin credential to create one short-lived room
and close it in a ``finally`` path. It emits only bounded endpoint facts and
boolean checks. It never prints a bearer token, link token, room id, name, or
response body.

Run:
    $env:WEFT_MCP_ORIGIN = "https://YOUR-VERIFIED-API-ORIGIN"
    $env:WEFT_MCP_LIFECYCLE_TOKEN = "<owner-or-admin-agent-key>"
    python -B scripts/probe_hosted_mcp_lifecycle.py --pretty

The lifecycle token must be separate from the read-only catalog token. The
room is a smoke-test resource and is closed before the probe reports success.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from typing import Any

try:  # Direct script execution puts ``scripts`` on sys.path.
    from scripts import probe_hosted_mcp_surface as _surface
except ImportError:  # pragma: no cover - exercised by direct CLI use.
    import probe_hosted_mcp_surface as _surface  # type: ignore[no-redef]


DEFAULT_TOKEN_ENV = "WEFT_MCP_LIFECYCLE_TOKEN"
MCP_PROTOCOL_VERSION = _surface.MCP_PROTOCOL_VERSION
SUPPORTED_PROTOCOL_VERSIONS = _surface.SUPPORTED_PROTOCOL_VERSIONS


def _structured(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    value = result.get("structuredContent")
    return value if isinstance(value, dict) else None


def probe(mcp_origin: str, token: str, timeout: float = 10.0) -> dict[str, Any]:
    origin = _surface._origin(mcp_origin)
    endpoint = f"{origin}/mcp"
    requests = ["initialize"]

    init_facts, init_result = _surface._rpc(
        endpoint,
        token,
        1,
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": "weft-hosted-mcp-lifecycle-probe",
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
    create_facts: dict[str, Any] = {
        "status": None,
        "content_type": "",
        "bytes": 0,
        "error": "not_attempted",
    }
    close_facts: dict[str, Any] = {
        "status": None,
        "content_type": "",
        "bytes": 0,
        "error": "not_attempted",
    }
    room_id: str | None = None
    room_create_ok = False
    room_close_ok = False

    if init_ok:
        requests.append("notifications/initialized")
        initialized_facts, initialized_ok = _surface._notify(
            endpoint,
            token,
            "notifications/initialized",
            timeout,
        )
    if init_ok and initialized_ok:
        requests.append("tools/list")
        tools_facts, tools_result = _surface._rpc(
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
    tool_name_set = set(tool_names)
    tools_catalog_ok = (
        tools_facts["status"] == 200
        and len(tool_names) == len(tool_name_set)
        and {"room_create", "room_close"} <= tool_name_set
    )

    if init_ok and initialized_ok and tools_catalog_ok:
        try:
            requests.append("tools/call:room_create")
            create_facts, create_result = _surface._rpc(
                endpoint,
                token,
                3,
                "tools/call",
                {
                    "name": "room_create",
                    "arguments": {
                        "cap": 2,
                        "name": f"release-smoke-{uuid.uuid4().hex[:12]}",
                        "ttl_seconds": 300,
                    },
                },
                timeout,
            )
            created = _structured(create_result)
            candidate_room_id = created.get("room_id") if created else None
            if isinstance(candidate_room_id, str) and candidate_room_id:
                room_id = candidate_room_id
                room_create_ok = (
                    create_facts["status"] == 200
                    and created.get("state") == "forming"
                )
        finally:
            # A successful create always gets a close attempt, even if a later
            # assertion changes. The room id is held only in memory and is
            # never included in the result or diagnostics.
            if room_id is not None:
                requests.append("tools/call:room_close")
                close_facts, close_result = _surface._rpc(
                    endpoint,
                    token,
                    4,
                    "tools/call",
                    {
                        "name": "room_close",
                        "arguments": {"room_id": room_id},
                    },
                    timeout,
                )
                closed = _structured(close_result)
                room_close_ok = bool(
                    close_facts["status"] == 200
                    and isinstance(closed, dict)
                    and closed.get("room_id") == room_id
                    and closed.get("state") == "closed"
                )

    attempted_facts = [init_facts, initialized_facts, tools_facts, create_facts, close_facts]
    reachable = any(facts["status"] is not None for facts in attempted_facts)
    credential_rejected = any(facts["status"] in {401, 403} for facts in attempted_facts)
    authenticated = init_facts["status"] == 200 and not credential_rejected
    checks = {
        "authenticated": authenticated,
        "initialize": init_ok,
        "initialized_notification": initialized_ok,
        "room_tools": tools_catalog_ok,
        "room_create": room_create_ok,
        "room_close": room_close_ok,
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
            "expected": "the dedicated lifecycle token must be accepted by POST /mcp",
            "initialize": _surface._facts(init_facts),
        })
    if not init_ok:
        diagnostics.append({
            "check": "initialize",
            "expected": "HTTP 200 JSON-RPC result with protocolVersion and serverInfo",
            "endpoint": _surface._facts(init_facts),
        })
    if not initialized_ok:
        diagnostics.append({
            "check": "initialized_notification",
            "expected": "HTTP 202 after a successful initialize response",
            "endpoint": _surface._facts(initialized_facts),
        })
    if not tools_catalog_ok:
        diagnostics.append({
            "check": "room_tools",
            "expected": "tools/list must expose room_create and room_close",
            "actual_count": len(tool_names),
            "missing_tools": sorted({"room_create", "room_close"} - tool_name_set),
            "endpoint": _surface._facts(tools_facts),
        })
    if not room_create_ok:
        diagnostics.append({
            "check": "room_create",
            "expected": "tools/call room_create returns a forming room",
            "endpoint": _surface._facts(create_facts),
        })
    if not room_close_ok:
        diagnostics.append({
            "check": "room_close",
            "expected": "tools/call room_close returns the created room in closed state",
            "endpoint": _surface._facts(close_facts),
        })

    return {
        "probe": "weft-hosted-mcp-lifecycle-v1",
        "origin": origin,
        "status": status,
        "checks": checks,
        "tool_count": len(tool_names),
        "diagnostics": diagnostics,
        "requests": requests,
        "endpoints": {
            "initialize": _surface._facts(init_facts),
            "initialized": _surface._facts(initialized_facts),
            "tools_list": _surface._facts(tools_facts),
            "room_create": _surface._facts(create_facts),
            "room_close": _surface._facts(close_facts),
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
            "probe": "weft-hosted-mcp-lifecycle-v1",
            "status": "INVALID_ARGUMENT",
            "error": "Explicit origin and token are required: " + ", ".join(missing) + ".",
        }
        print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
        return 4
    try:
        result = probe(origin, token, timeout=args.timeout)
    except ValueError as exc:
        result = {
            "probe": "weft-hosted-mcp-lifecycle-v1",
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
