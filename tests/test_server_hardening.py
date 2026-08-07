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

from weft_mcp.core import WeftStore
from weft_mcp.server import WeftDispatcher, _MCPRequestHandler, _Metrics, _WindowRateLimiter


class PublicJoinScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root)
        self.unscoped = WeftDispatcher(self.store)
        self.unscoped.call_tool("finalisma_register_agent", {"team_id": "team-a", "agent_id": "agent-a"})
        self.unscoped.call_tool("finalisma_register_agent", {"team_id": "team-b", "agent_id": "agent-b"})
        self.scoped = WeftDispatcher(self.store, team_scope="team-a")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_public_join_cannot_bypass_the_http_coordinator_team_scope(self) -> None:
        pairing = self.unscoped.call_tool("finalisma_create_pairing", {"team_id": "team-b", "initiator_id": "agent-b"})
        handler = type("ScopedPublicJoinHandler", (_MCPRequestHandler,), {})
        handler.dispatcher = self.scoped
        handler.token = None
        handler.allowed_origins = {"http://localhost"}
        handler.rate_limiter = _WindowRateLimiter()
        handler.metrics = _Metrics()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            body = json.dumps({"token": pairing["join_token"], "agent_id": "joining-agent", "consent": True})
            connection = HTTPConnection(host, port, timeout=5)
            connection.request(
                "POST",
                urlsplit(pairing["join_url"]).path,
                body=body,
                headers={"Content-Type": "application/json", "Origin": "http://localhost"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()

            self.assertEqual(response.status, 403)
            self.assertEqual(payload["error"]["code"], "team_scope_forbidden")
            self.assertNotIn(pairing["join_token"], json.dumps(payload))
            self.assertEqual(self.store.pairing_preview(pairing["join_token"])["status"], "issued")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
