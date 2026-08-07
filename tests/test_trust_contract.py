from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftError, WeftStore
from weft_mcp.server import WeftDispatcher, _MCPRequestHandler, _Metrics, _WindowRateLimiter, handle_json_rpc


class StrictConsentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WeftStore(self.root / "state.db", self.root)
        self.store.register_agent("demo", "agent-a", role="architect")
        self.dispatcher = WeftDispatcher(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_core_requires_literal_boolean_true(self) -> None:
        pairing = self.store.create_pairing("agent-a", "demo")
        for malformed in ("false", "yes", 1, 0, None, [], {}):
            with self.subTest(consent=malformed), self.assertRaises(WeftError) as rejected:
                self.store.join_pairing(pairing["join_token"], "agent-b", consent=malformed)  # type: ignore[arg-type]
            self.assertEqual(rejected.exception.code, "invalid_consent")
            self.assertEqual(self.store.pairing_preview(pairing["join_token"])["status"], "issued")

        with self.assertRaises(WeftError) as declined:
            self.store.join_pairing(pairing["join_token"], "agent-b", consent=False)
        self.assertEqual(declined.exception.code, "consent_required")
        self.assertEqual(self.store.join_pairing(pairing["join_token"], "agent-b", consent=True)["state"], "active")

    def test_mcp_tool_call_does_not_coerce_malformed_consent(self) -> None:
        pairing = self.store.create_pairing("agent-a", "demo")
        for request_id, malformed in enumerate(("false", "yes", 1), start=1):
            response = handle_json_rpc(
                self.dispatcher,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "finalisma_join_pairing",
                        "arguments": {
                            "token": pairing["join_token"],
                            "agent_id": "agent-b",
                            "consent": malformed,
                        },
                    },
                },
            )
            self.assertIsNotNone(response)
            tool_result = response["result"]
            self.assertTrue(tool_result["isError"])
            error = json.loads(tool_result["content"][0]["text"])["error"]
            self.assertEqual(error["code"], "invalid_consent")
        self.assertEqual(self.store.pairing_preview(pairing["join_token"])["status"], "issued")

    def test_public_http_join_does_not_coerce_malformed_consent(self) -> None:
        pairing = self.store.create_pairing("agent-a", "demo")
        handler = type("StrictConsentHTTPHandler", (_MCPRequestHandler,), {})
        handler.dispatcher = self.dispatcher
        handler.token = None
        handler.allowed_origins = {"http://localhost"}
        handler.rate_limiter = _WindowRateLimiter(limit=20, window_seconds=60)
        handler.metrics = _Metrics()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            join_path = urlsplit(pairing["join_url"]).path
            for malformed in ("false", "yes", 1):
                body = json.dumps({"token": pairing["join_token"], "agent_id": "agent-b", "consent": malformed})
                connection = HTTPConnection(host, port, timeout=5)
                connection.request(
                    "POST",
                    join_path,
                    body=body,
                    headers={"Content-Type": "application/json", "Origin": "http://localhost"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                with self.subTest(consent=malformed):
                    self.assertEqual(response.status, 400)
                    self.assertEqual(payload["error"]["code"], "invalid_consent")

            valid_body = json.dumps({"token": pairing["join_token"], "agent_id": "agent-b", "consent": True})
            connection = HTTPConnection(host, port, timeout=5)
            connection.request(
                "POST",
                join_path,
                body=valid_body,
                headers={"Content-Type": "application/json", "Origin": "http://localhost"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["state"], "active")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
