"""Integration tests for the Finalisma Bridge MCP surface.

These tests drive the bridge adapters (WebhookBridge, PollingBridge,
ClipboardBridge) THROUGH the real MCP JSON-RPC dispatcher — never via the
bridge.py Python API directly. The finalisma_bridge_* tools are registered by
the orchestrator; this file asserts the contract those tools must satisfy.

RED-only: this file is the failing-test deliverable. The orchestrator wires
the tools after these tests exist.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finalisma_mcp.core import FinalismaError, FinalismaStore
from finalisma_mcp.server import FinalismaDispatcher, TOOLS


class BridgeIntegrationTests(unittest.TestCase):
    """End-to-end bridge contract exercised via FinalismaDispatcher.call_tool."""

    BRIDGE_TOOL_NAMES = {
        "finalisma_bridge_poll",
        "finalisma_bridge_ack",
        "finalisma_bridge_webhook_register",
        "finalisma_bridge_bootstrap",
    }

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = FinalismaStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = FinalismaDispatcher(self.store)
        # Real registration flow to obtain a bound actor token.
        registered = self.dispatcher.call_tool(
            "finalisma_register_agent",
            {"team_id": "team-1", "agent_id": "agent-1", "role": "architect"},
        )
        self.actor_token = registered["actor_token"]
        self.team_id = "team-1"
        self.agent_id = "agent-1"

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _call_bridge(self, tool_name: str, **kwargs) -> dict:
        """Invoke a bridge tool through the MCP dispatcher."""
        return self.dispatcher.call_tool(tool_name, kwargs)

    # ------------------------------------------------------------------
    # 1. Webhook registration returns webhook_id; secret never returned.
    # ------------------------------------------------------------------
    def test_webhook_register_returns_webhook_id_and_hides_secret(self) -> None:
        secret_ref = "whsec_live_secret_value_do_not_leak_12345"
        result = self._call_bridge(
            "finalisma_bridge_webhook_register",
            team_id=self.team_id,
            agent_id=self.agent_id,
            actor_token=self.actor_token,
            url="https://example.com/finalisma/webhook",
            secret_ref=secret_ref,
        )
        self.assertIn("webhook_id", result)
        self.assertTrue(result["webhook_id"].startswith("wh_"))
        self.assertEqual(result["url"], "https://example.com/finalisma/webhook")
        self.assertTrue(result["active"])
        # The secret_ref value must NEVER appear in the result JSON.
        serialized = json.dumps(result)
        self.assertNotIn(secret_ref, serialized)

    # ------------------------------------------------------------------
    # 2. webhook_register with WRONG actor_token raises FinalismaError.
    # ------------------------------------------------------------------
    def test_webhook_register_wrong_token_raises(self) -> None:
        with self.assertRaises(FinalismaError) as ctx:
            self._call_bridge(
                "finalisma_bridge_webhook_register",
                team_id=self.team_id,
                agent_id=self.agent_id,
                actor_token="fst_actor_wrong_token_value_that_is_long_enough_123456",
                url="https://example.com/finalisma/webhook",
                secret_ref="whsec_live_secret_value_do_not_leak_12345",
            )
        self.assertEqual(ctx.exception.code, "actor_auth_invalid")

    # ------------------------------------------------------------------
    # 3. poll -> ack -> poll-again: at-most-once delivery.
    # ------------------------------------------------------------------
    def test_poll_ack_poll_no_repeats_at_most_once(self) -> None:
        # First poll returns a cursor (even if empty).
        first_poll = self._call_bridge(
            "finalisma_bridge_poll",
            team_id=self.team_id,
            agent_id=self.agent_id,
            actor_token=self.actor_token,
            cursor=0,
        )
        self.assertIn("events", first_poll)
        self.assertIn("cursor", first_poll)
        cursor_after_first = first_poll["cursor"]

        # If there are events, ack them and verify no repeats.
        if first_poll["events"]:
            event_ids = [e["event_id"] for e in first_poll["events"]]
            ack_result = self._call_bridge(
                "finalisma_bridge_ack",
                team_id=self.team_id,
                agent_id=self.agent_id,
                actor_token=self.actor_token,
                event_ids=event_ids,
            )
            self.assertEqual(ack_result["acked"], event_ids)

            # Poll again after ack — must not return the same events.
            second_poll = self._call_bridge(
                "finalisma_bridge_poll",
                team_id=self.team_id,
                agent_id=self.agent_id,
                actor_token=self.actor_token,
                cursor=cursor_after_first,
            )
            self.assertEqual(second_poll["events"], [])
        else:
            # Empty outbox path: cursor should still be present and >= 0.
            self.assertGreaterEqual(cursor_after_first, 0)

    # ------------------------------------------------------------------
    # 4. bootstrap returns a parseable snippet; no raw secret in cleartext.
    # ------------------------------------------------------------------
    def test_bootstrap_returns_parseable_snippet_no_cleartext_secret(self) -> None:
        endpoint = "https://bridge.example.com"
        result = self._call_bridge(
            "finalisma_bridge_bootstrap",
            team_id=self.team_id,
            agent_id=self.agent_id,
            actor_token=self.actor_token,
            endpoint=endpoint,
        )
        self.assertIn("bootstrap", result)
        snippet = result["bootstrap"]
        # Snippet must be parseable JSON.
        parsed = json.loads(snippet)
        self.assertIn("endpoint", parsed)
        self.assertIn("team_id", parsed)
        self.assertEqual(parsed["endpoint"], endpoint)
        self.assertEqual(parsed["team_id"], self.team_id)
        # The snippet must not embed a raw secret in cleartext beyond the
        # join_token the caller supplied (the join_token is allowed; any
        # server-side secret is not).
        snippet_text = json.dumps(parsed)
        # Assert none of these hypothetical secret markers leak.
        self.assertNotIn("whsec_", snippet_text)
        self.assertNotIn("secret_ref", snippet_text)
        self.assertNotIn("signing_secret", snippet_text)

    # ------------------------------------------------------------------
    # 5. All four bridge tool schemas present in TOOLS.
    # ------------------------------------------------------------------
    def test_bridge_tool_schemas_present_in_tools_list(self) -> None:
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        for tool_name in self.BRIDGE_TOOL_NAMES:
            self.assertIn(tool_name, schemas, f"Missing tool schema: {tool_name}")
            schema = schemas[tool_name]
            self.assertEqual(schema["type"], "object")
            self.assertIn("properties", schema)
            # actor_token must be a declared property on every bridge tool.
            self.assertIn("actor_token", schema["properties"], tool_name)

    # ------------------------------------------------------------------
    # 6. Wrong-token negative: bridge_poll with bad token raises.
    # ------------------------------------------------------------------
    def test_bridge_poll_wrong_token_raises(self) -> None:
        with self.assertRaises(FinalismaError) as ctx:
            self._call_bridge(
                "finalisma_bridge_poll",
                team_id=self.team_id,
                agent_id=self.agent_id,
                actor_token="fst_actor_wrong_token_value_that_is_long_enough_123456",
                cursor=0,
            )
        self.assertEqual(ctx.exception.code, "actor_auth_invalid")


if __name__ == "__main__":
    unittest.main()
