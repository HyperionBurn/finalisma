from __future__ import annotations

import io
import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from scripts import probe_hosted_mcp_surface


class _MCPHandler(BaseHTTPRequestHandler):
    expected_token = "probe-secret"
    mode = "pass"
    tool_names = list(probe_hosted_mcp_surface.EXPECTED_TOOL_NAMES)
    calls: list[str] = []
    accepts: list[str | None] = []

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        method = request.get("method")
        type(self).calls.append(method)
        type(self).accepts.append(self.headers.get("Accept"))
        if self.headers.get("Authorization") != f"Bearer {type(self).expected_token}":
            self._reply(401, {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32001, "message": "SECRET_BODY"},
            })
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if type(self).mode == "malformed":
            body = b"SECRET_BODY"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if type(self).mode == "oversized":
            body = b"x" * (probe_hosted_mcp_surface.MAX_RESPONSE_BYTES + 1)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "initialize":
            self._reply(200, {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "protocolVersion": probe_hosted_mcp_surface.MCP_PROTOCOL_VERSION,
                    "serverInfo": {"name": "weft", "version": "test"},
                },
            })
            return
        if method == "tools/list":
            self._reply(200, {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "tools": [
                        {"name": name, "description": "test", "inputSchema": {}}
                        for name in type(self).tool_names
                    ],
                },
            })
            return
        self._reply(200, {"jsonrpc": "2.0", "id": request.get("id"), "result": {}})

    def log_message(self, *_args) -> None:
        return


def _serve(
    *,
    mode: str = "pass",
    tool_names: list[str] | None = None,
) -> tuple[ThreadingHTTPServer, str]:
    handler = type(
        "HostedMCPProbeHandler",
        (_MCPHandler,),
        {
            "mode": mode,
            "tool_names": list(tool_names or probe_hosted_mcp_surface.EXPECTED_TOOL_NAMES),
            "calls": [],
            "accepts": [],
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    return server, origin


class HostedMCPSurfaceProbeTests(unittest.TestCase):
    def test_current_catalog_passes_and_only_read_only_methods_are_sent(self) -> None:
        server, origin = _serve()
        try:
            result = probe_hosted_mcp_surface.probe(origin, "probe-secret", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["tool_count"], 15)
        self.assertEqual(
            result["requests"],
            ["initialize", "notifications/initialized", "tools/list"],
        )
        self.assertEqual(
            server.RequestHandlerClass.calls,
            ["initialize", "notifications/initialized", "tools/list"],
        )
        self.assertEqual(
            server.RequestHandlerClass.accepts,
            ["application/json, text/event-stream"] * 3,
        )

    def test_stale_catalog_reports_missing_room_management_tools(self) -> None:
        stale = [
            name for name in probe_hosted_mcp_surface.EXPECTED_TOOL_NAMES
            if name not in {"room_list", "room_close"}
        ]
        server, origin = _serve(tool_names=stale)
        try:
            result = probe_hosted_mcp_surface.probe(origin, "probe-secret", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result["status"], "DRIFT")
        self.assertEqual(result["diagnostics"][0]["check"], "tools_catalog")
        self.assertEqual(
            result["diagnostics"][0]["missing_tools"],
            ["room_close", "room_list"],
        )

    def test_auth_failure_does_not_echo_token_or_response_body(self) -> None:
        server, origin = _serve()
        try:
            result = probe_hosted_mcp_surface.probe(origin, "wrong-secret", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
        rendered = json.dumps(result)
        self.assertEqual(result["status"], "UNAUTHORIZED")
        self.assertIn("authenticated", rendered)
        self.assertNotIn("wrong-secret", rendered)
        self.assertNotIn("SECRET_BODY", rendered)

    def test_malformed_response_is_redacted(self) -> None:
        server, origin = _serve(mode="malformed")
        try:
            result = probe_hosted_mcp_surface.probe(origin, "probe-secret", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result["status"], "DRIFT")
        self.assertNotIn("SECRET_BODY", json.dumps(result))

    def test_oversized_response_is_rejected_without_buffering_the_body(self) -> None:
        server, origin = _serve(mode="oversized")
        try:
            result = probe_hosted_mcp_surface.probe(origin, "probe-secret", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result["status"], "DRIFT")
        self.assertEqual(result["endpoints"]["initialize"]["error"], "response_too_large")
        self.assertLessEqual(
            result["endpoints"]["initialize"]["bytes"],
            probe_hosted_mcp_surface.MAX_RESPONSE_BYTES + 1,
        )

    def test_invalid_initialize_response_stops_before_normal_operations(self) -> None:
        server, origin = _serve(mode="malformed")
        original = server.RequestHandlerClass.do_POST

        def bad_initialize(handler: BaseHTTPRequestHandler) -> None:
            length = int(handler.headers.get("Content-Length", "0"))
            request = json.loads(handler.rfile.read(length))
            if request.get("method") == "initialize":
                type(handler).calls.append("initialize")
                type(handler).accepts.append(handler.headers.get("Accept"))
                body = json.dumps({
                    "jsonrpc": "2.0",
                    "id": 99,
                    "result": {
                        "protocolVersion": "bogus",
                        "serverInfo": {"name": "weft", "version": "test"},
                    },
                }).encode("utf-8")
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
                return
            original(handler)

        server.RequestHandlerClass.do_POST = bad_initialize
        try:
            result = probe_hosted_mcp_surface.probe(origin, "probe-secret", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result["status"], "DRIFT")
        self.assertFalse(result["checks"]["initialize"])
        self.assertEqual(result["requests"], ["initialize"])

    def test_cli_requires_explicit_origin_and_token_without_echoing_token(self) -> None:
        output = io.StringIO()
        with patch.dict(
            os.environ,
            {"WEFT_MCP_ORIGIN": "", "WEFT_MCP_PROBE_TOKEN": ""},
            clear=False,
        ):
            with redirect_stdout(output):
                code = probe_hosted_mcp_surface.main([])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 4)
        self.assertEqual(result["status"], "INVALID_ARGUMENT")
        self.assertIn("WEFT_MCP_ORIGIN", result["error"])
        self.assertIn("WEFT_MCP_PROBE_TOKEN", result["error"])
        self.assertNotIn("probe-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
