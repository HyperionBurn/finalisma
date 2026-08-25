from __future__ import annotations
from tests._server_readiness import await_serving as _await_serving

import io
import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from scripts import probe_hosted_mcp_lifecycle
from scripts import probe_hosted_mcp_surface


class _LifecycleHandler(BaseHTTPRequestHandler):
    expected_token = "lifecycle-secret"
    mode = "pass"
    calls: list[str] = []

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
            names = list(probe_hosted_mcp_surface.EXPECTED_TOOL_NAMES)
            if type(self).mode == "missing-tools":
                names = ["room_list"]
            self._reply(200, {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"tools": [{"name": name} for name in names]},
            })
            return
        if method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            if name == "room_create":
                if type(self).mode == "create-error":
                    self._reply(200, {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "error": {"code": -32002, "message": "SECRET_BODY"},
                    })
                    return
                content = {"room_id": "room_smoke_secret", "state": "forming", "link_token": "rm_secret"}
                if type(self).mode == "malformed-create":
                    content = {"state": "forming", "link_token": "rm_secret"}
                self._reply(200, {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": {"structuredContent": content, "content": []},
                })
                return
            if name == "room_close":
                if type(self).mode == "close-error":
                    self._reply(200, {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "error": {"code": -32003, "message": "SECRET_BODY"},
                    })
                    return
                self._reply(200, {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": {
                        "structuredContent": {
                            "room_id": "room_smoke_secret",
                            "state": "closed",
                        },
                        "content": [],
                    },
                })
                return
        self._reply(200, {"jsonrpc": "2.0", "id": request.get("id"), "result": {}})

    def log_message(self, *_args) -> None:
        return


def _serve(mode: str = "pass") -> tuple[ThreadingHTTPServer, str]:
    handler = type(
        "HostedMCPLifecycleProbeHandler",
        (_LifecycleHandler,),
        {"mode": mode, "calls": []},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _await_serving(server)
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class HostedMCPLifecycleProbeTests(unittest.TestCase):
    def _run(self, mode: str = "pass", token: str = "lifecycle-secret") -> tuple[dict, list[str]]:
        server, origin = _serve(mode)
        try:
            result = probe_hosted_mcp_lifecycle.probe(origin, token, timeout=2)
            calls = list(server.RequestHandlerClass.calls)
        finally:
            server.shutdown()
            server.server_close()
        return result, calls

    def test_create_is_closed_and_output_is_redacted(self) -> None:
        result, calls = self._run()
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(
            result["requests"],
            [
                "initialize",
                "notifications/initialized",
                "tools/list",
                "tools/call:room_create",
                "tools/call:room_close",
            ],
        )
        self.assertEqual(calls, ["initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call"])
        rendered = json.dumps(result)
        for secret in ("room_smoke_secret", "rm_secret", "release-smoke-"):
            self.assertNotIn(secret, rendered)

    def test_close_error_fails_after_attempting_cleanup(self) -> None:
        result, calls = self._run("close-error")
        self.assertEqual(result["status"], "DRIFT")
        self.assertTrue(result["checks"]["room_create"])
        self.assertFalse(result["checks"]["room_close"])
        self.assertEqual(calls[-1], "tools/call")
        self.assertNotIn("SECRET_BODY", json.dumps(result))

    def test_create_error_does_not_claim_success_or_close_unknown_room(self) -> None:
        result, calls = self._run("create-error")
        self.assertEqual(result["status"], "DRIFT")
        self.assertFalse(result["checks"]["room_create"])
        self.assertFalse(result["checks"]["room_close"])
        self.assertEqual(calls, ["initialize", "notifications/initialized", "tools/list", "tools/call"])
        self.assertNotIn("SECRET_BODY", json.dumps(result))

    def test_malformed_create_result_fails_without_leaking_link_token(self) -> None:
        result, calls = self._run("malformed-create")
        self.assertEqual(result["status"], "DRIFT")
        self.assertFalse(result["checks"]["room_create"])
        self.assertFalse(result["checks"]["room_close"])
        self.assertEqual(calls[-1], "tools/call")
        self.assertNotIn("rm_secret", json.dumps(result))

    def test_missing_room_tools_stops_before_mutation(self) -> None:
        result, calls = self._run("missing-tools")
        self.assertEqual(result["status"], "DRIFT")
        self.assertFalse(result["checks"]["room_tools"])
        self.assertEqual(calls, ["initialize", "notifications/initialized", "tools/list"])

    def test_auth_failure_is_redacted(self) -> None:
        result, calls = self._run(token="wrong-secret")
        self.assertEqual(result["status"], "UNAUTHORIZED")
        self.assertEqual(calls, ["initialize"])
        rendered = json.dumps(result)
        self.assertNotIn("wrong-secret", rendered)
        self.assertNotIn("SECRET_BODY", rendered)

    def test_cli_requires_dedicated_origin_and_token(self) -> None:
        output = io.StringIO()
        with patch.dict(
            os.environ,
            {"WEFT_MCP_ORIGIN": "", "WEFT_MCP_LIFECYCLE_TOKEN": ""},
            clear=False,
        ):
            with redirect_stdout(output):
                code = probe_hosted_mcp_lifecycle.main([])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 4)
        self.assertEqual(result["status"], "INVALID_ARGUMENT")
        self.assertIn("WEFT_MCP_LIFECYCLE_TOKEN", result["error"])
        self.assertNotIn("lifecycle-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
