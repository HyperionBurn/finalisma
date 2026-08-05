from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finalisma_mcp.__main__ import _resolve_actor_auth
from finalisma_mcp.core import FinalismaError, FinalismaStore
from finalisma_mcp.server import FinalismaDispatcher, TOOLS, _MCPRequestHandler, _Metrics, _WindowRateLimiter, run_stdio


class ActorCredentialTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = FinalismaStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = FinalismaDispatcher(self.store)
        self.agent_a = self.dispatcher.call_tool(
            "finalisma_register_agent",
            {"team_id": "demo", "agent_id": "agent-a", "role": "architect"},
        )
        self.agent_b = self.dispatcher.call_tool(
            "finalisma_register_agent",
            {"team_id": "demo", "agent_id": "agent-b", "role": "builder"},
        )
        self.token_a = self.agent_a["actor_token"]
        self.token_b = self.agent_b["actor_token"]

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_schemas_and_dispatcher_cover_actor_credential_contract(self) -> None:
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        actor_tools = {
            "finalisma_create_pairing",
            "finalisma_join_pairing",
            "finalisma_register_agent",
            "finalisma_route_task",
            "finalisma_team_status",
            "finalisma_create_task",
            "finalisma_claim_task",
            "finalisma_update_task",
            "finalisma_send_message",
            "finalisma_read_inbox",
            "finalisma_ack_message",
            "finalisma_heartbeat",
            "finalisma_verify_task",
            "finalisma_complete_task",
        }
        for tool_name in actor_tools:
            self.assertIn("actor_token", schemas[tool_name]["properties"], tool_name)
        self.assertIn("current_token", schemas["finalisma_rotate_agent_credential"]["properties"])
        self.assertIn("agent_id", schemas["finalisma_route_task"]["properties"])
        self.assertIn("agent_id", schemas["finalisma_team_status"]["properties"])

    def test_required_mode_issues_and_enforces_bound_actor_tokens(self) -> None:
        self.assertTrue(self.token_a.startswith("fst_actor_"))
        with self.assertRaises(FinalismaError) as missing:
            self.dispatcher.call_tool("finalisma_register_agent", {"team_id": "demo", "agent_id": "agent-a", "role": "architect"})
        self.assertEqual(missing.exception.code, "actor_auth_required")

        with self.assertRaises(FinalismaError) as wrong:
            self.dispatcher.call_tool("finalisma_create_task", {"team_id": "demo", "created_by": "agent-a", "title": "Wrong proof", "actor_token": "fst_actor_wrong_token_value_which_is_long_enough"})
        self.assertEqual(wrong.exception.code, "actor_auth_invalid")

        with self.assertRaises(FinalismaError) as cross_agent:
            self.dispatcher.call_tool("finalisma_create_task", {"team_id": "demo", "created_by": "agent-b", "title": "Spoof agent", "actor_token": self.token_a})
        self.assertEqual(cross_agent.exception.code, "actor_auth_invalid")

        created = self.dispatcher.call_tool(
            "finalisma_create_task",
            {"team_id": "demo", "created_by": "agent-a", "title": "Bound proof", "actor_token": self.token_a},
        )
        self.assertTrue(created["created"])

    def test_existing_identity_join_needs_its_token_and_http_does_not_leak_it(self) -> None:
        missing_proof_pairing = self.dispatcher.call_tool(
            "finalisma_create_pairing",
            {"team_id": "demo", "initiator_id": "agent-a", "actor_token": self.token_a},
        )
        with self.assertRaises(FinalismaError) as overwrite:
            self.dispatcher.call_tool(
                "finalisma_join_pairing",
                {"token": missing_proof_pairing["join_token"], "agent_id": "agent-b", "consent": True},
            )
        self.assertEqual(overwrite.exception.code, "actor_auth_required")

        pairing = self.dispatcher.call_tool(
            "finalisma_create_pairing",
            {"team_id": "demo", "initiator_id": "agent-a", "actor_token": self.token_a},
        )
        handler = type("ActorJoinHandler", (_MCPRequestHandler,), {})
        handler.dispatcher = self.dispatcher
        handler.token = None
        handler.allowed_origins = {"http://localhost"}
        handler.rate_limiter = _WindowRateLimiter()
        handler.metrics = _Metrics()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            body = json.dumps({"token": pairing["join_token"], "agent_id": "agent-b", "actor_token": self.token_b, "consent": True})
            connection = HTTPConnection(host, port, timeout=5)
            connection.request("POST", urlsplit(pairing["join_url"]).path, body=body, headers={"Content-Type": "application/json", "Origin": "http://localhost"})
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["state"], "active")
            self.assertNotIn(self.token_b, json.dumps(payload))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_rotation_invalidates_the_prior_token(self) -> None:
        rotated = self.dispatcher.call_tool(
            "finalisma_rotate_agent_credential",
            {"team_id": "demo", "agent_id": "agent-a", "current_token": self.token_a},
        )
        replacement = rotated["actor_token"]
        self.assertNotEqual(replacement, self.token_a)
        with self.assertRaises(FinalismaError) as stale:
            self.dispatcher.call_tool("finalisma_create_task", {"team_id": "demo", "created_by": "agent-a", "title": "Stale", "actor_token": self.token_a})
        self.assertEqual(stale.exception.code, "actor_auth_invalid")
        created = self.dispatcher.call_tool("finalisma_create_task", {"team_id": "demo", "created_by": "agent-a", "title": "Rotated", "actor_token": replacement})
        self.assertTrue(created["created"])

    def test_trusted_stdio_and_cli_actor_auth_resolution(self) -> None:
        trusted_store = FinalismaStore(Path(self.temp.name) / "trusted.db", Path(self.temp.name) / "trusted-workspace")
        trusted_dispatcher = FinalismaDispatcher(trusted_store)
        trusted_dispatcher.call_tool("finalisma_register_agent", {"team_id": "trusted", "agent_id": "local-agent"})
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "finalisma_create_task", "arguments": {"team_id": "trusted", "created_by": "local-agent", "title": "No token in stdio trust mode"}}}
        output = io.StringIO()
        run_stdio(trusted_dispatcher, io.StringIO(json.dumps(request) + "\n"), output)
        self.assertTrue(json.loads(output.getvalue())["result"]["structuredContent"]["created"])

        self.assertTrue(_resolve_actor_auth("http", "auto", "127.0.0.1"))
        self.assertFalse(_resolve_actor_auth("stdio", "auto", "127.0.0.1"))
        warning = io.StringIO()
        with redirect_stderr(warning):
            self.assertFalse(_resolve_actor_auth("http", "trust", "localhost"))
        self.assertIn("security warning", warning.getvalue())
        with self.assertRaises(FinalismaError) as rejected:
            _resolve_actor_auth("http", "trust", "0.0.0.0")
        self.assertEqual(rejected.exception.code, "actor_auth_trust_forbidden")


if __name__ == "__main__":
    unittest.main()
