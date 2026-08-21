"""Explicit hosted-MCP agent-key regression coverage.

The broader hosted MCP suite historically used only ``fss_`` sessions. These
tests keep the long-lived credential path visible by name: create an ``agk_``
key with an interactive session, use it through hosted MCP, revoke it with the
session, and prove the very next MCP request is refused.
"""

from __future__ import annotations

import sys
import unittest
from http import HTTPStatus
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from test_hosted_mcp import HostedMCPTestBase, _mcp, _post  # noqa: E402


class HostedMCPAgentKeyTests(HostedMCPTestBase):
    def test_agent_key_authenticates_mcp_and_revocation_is_immediate(self) -> None:
        account = self._signup("hosted-agent-key@example.com")
        session = account["session_token"]
        status, key = _post(
            self.base,
            "/v1/agent-keys",
            {"label": "hosted-mcp"},
            token=session,
        )
        self.assertEqual(status, HTTPStatus.CREATED)
        agent_key = key["agent_key"]
        key_id = key["key_id"]

        status, initialized = _mcp(self.base, "initialize", {"capabilities": {}}, token=agent_key)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIn("serverInfo", initialized["result"])

        status, listing = _mcp(self.base, "tools/list", None, token=agent_key)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(len(listing["result"]["tools"]), 14)

        status, created = _mcp(
            self.base,
            "tools/call",
            {"name": "room_create", "arguments": {"cap": 2, "name": "agent-key"}},
            token=agent_key,
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(created["result"].get("isError"))
        created_room = created["result"]["structuredContent"]
        self.assertTrue(created_room["room_id"].startswith("room_"))
        self.assertEqual(created_room["owner_agent_id"], account["account_id"])

        status, revoked = _post(
            self.base,
            "/v1/agent-keys/revoke",
            {"key_id": key_id},
            token=session,
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIs(revoked["revoked"], True)

        status, refused = _mcp(self.base, "ping", None, token=agent_key)
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(refused["error"]["message"], "Unauthorized")

    def test_agent_key_can_list_and_close_own_room_after_join(self) -> None:
        account = self._signup("hosted-agent-key-owner@example.com")
        session = account["session_token"]
        status, key = _post(
            self.base,
            "/v1/agent-keys",
            {"label": "hosted-mcp-owner"},
            token=session,
        )
        self.assertEqual(status, HTTPStatus.CREATED)
        agent_key = key["agent_key"]

        status, created = _mcp(
            self.base,
            "tools/call",
            {"name": "room_create", "arguments": {"cap": 15, "name": "owner"}},
            token=agent_key,
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(created["result"].get("isError"))
        room = created["result"]["structuredContent"]

        status, joined = _mcp(
            self.base,
            "tools/call",
            {"name": "room_join", "arguments": {
                "room_id": room["room_id"],
                "link_token": room["link_token"],
                "consent": True,
            }},
            token=agent_key,
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(joined["result"].get("isError"))

        status, listing = _mcp(
            self.base,
            "tools/call",
            {"name": "room_list", "arguments": {}},
            token=agent_key,
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(listing["result"].get("isError"))
        rooms = listing["result"]["structuredContent"]["rooms"]
        self.assertIn(room["room_id"], {entry["room_id"] for entry in rooms})

        status, closed = _mcp(
            self.base,
            "tools/call",
            {"name": "room_close", "arguments": {"room_id": room["room_id"]}},
            token=agent_key,
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(closed["result"].get("isError"))
        self.assertEqual(closed["result"]["structuredContent"]["state"], "closed")

        status, repeated = _mcp(
            self.base,
            "tools/call",
            {"name": "room_close", "arguments": {"room_id": room["room_id"]}},
            token=agent_key,
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertFalse(repeated["result"].get("isError"))
        self.assertEqual(repeated["result"]["structuredContent"]["state"], "closed")


if __name__ == "__main__":
    unittest.main()
