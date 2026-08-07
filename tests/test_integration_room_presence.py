"""Integration tests for the Room presence + roster-visibility contract.

RED deliverable — these tests assert the finalisma_room_* MCP surface per
docs/ROOMS_DESIGN.md §4 (presence), §8 (tool surface), §9 (negative cases).
Every finalisma_room_* call MUST fail with WeftError("unknown_tool", ...)
until the orchestrator wires the room tools into the dispatcher and TOOLS list.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftError, WeftStore
from weft_mcp.server import WeftDispatcher, TOOLS


class RoomPresenceIntegrationTests(unittest.TestCase):
    """Presence + roster-visibility contract through the real MCP surface."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        self.team = "team-presence"
        # Register three agents and capture their actor tokens.
        reg_a1 = self.dispatcher.call_tool(
            "finalisma_register_agent",
            {"team_id": self.team, "agent_id": "A1", "role": "owner"},
        )
        reg_a2 = self.dispatcher.call_tool(
            "finalisma_register_agent",
            {"team_id": self.team, "agent_id": "A2", "role": "member"},
        )
        reg_a3 = self.dispatcher.call_tool(
            "finalisma_register_agent",
            {"team_id": self.team, "agent_id": "A3", "role": "member"},
        )
        self.token_a1 = reg_a1["actor_token"]
        self.token_a2 = reg_a2["actor_token"]
        self.token_a3 = reg_a3["actor_token"]

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    # -- helpers -----------------------------------------------------------

    def _create_room(self, cap: int = 5) -> dict:
        return self.dispatcher.call_tool(
            "finalisma_room_create",
            {"team_id": self.team, "owner_agent_id": "A1", "cap": cap},
        )

    def _join(self, room_id: str, link_token: str, agent_id: str, token: str,
              capabilities: list[str] | None = None) -> dict:
        args = {
            "team_id": self.team,
            "room_id": room_id,
            "link_token": link_token,
            "agent_id": agent_id,
            "consent": True,
            "actor_token": token,
        }
        if capabilities is not None:
            args["capabilities"] = capabilities
        return self.dispatcher.call_tool("finalisma_room_join", args)

    def _info(self, room_id: str, agent_id: str, token: str) -> dict:
        return self.dispatcher.call_tool(
            "finalisma_room_info",
            {"team_id": self.team, "room_id": room_id, "agent_id": agent_id, "actor_token": token},
        )

    def _heartbeat(self, room_id: str, agent_id: str, token: str) -> dict:
        return self.dispatcher.call_tool(
            "finalisma_room_heartbeat",
            {"team_id": self.team, "room_id": room_id, "agent_id": agent_id, "actor_token": token},
        )

    def _leave(self, room_id: str, agent_id: str, token: str) -> dict:
        return self.dispatcher.call_tool(
            "finalisma_room_leave",
            {"team_id": self.team, "room_id": room_id, "agent_id": agent_id, "actor_token": token},
        )

    # -- tests -------------------------------------------------------------

    def test_info_shows_all_members_with_capabilities_and_timestamps(self) -> None:
        """Test 1: info shows all 3 members active, capabilities round-trip,
        last_seen and joined_at present."""
        room = self._create_room(cap=5)
        self._join(room["room_id"], room["link_token"], "A1", self.token_a1,
                   capabilities=["read", "write"])
        self._join(room["room_id"], room["link_token"], "A2", self.token_a2,
                   capabilities=["read"])
        self._join(room["room_id"], room["link_token"], "A3", self.token_a3,
                   capabilities=["read", "admin"])

        info = self._info(room["room_id"], "A1", self.token_a1)
        self.assertEqual(info["room_id"], room["room_id"])
        self.assertEqual(info["member_count"], 3)
        self.assertEqual(info["owner_agent_id"], "A1")

        members_by_id = {m["agent_id"]: m for m in info["members"]}
        self.assertEqual(set(members_by_id), {"A1", "A2", "A3"})

        for agent_id, expected_caps in [("A1", ["read", "write"]),
                                        ("A2", ["read"]),
                                        ("A3", ["read", "admin"])]:
            member = members_by_id[agent_id]
            self.assertEqual(member["status"], "active")
            self.assertEqual(member["capabilities"], expected_caps)
            self.assertIn("last_seen", member)
            self.assertIn("joined_at", member)
            self.assertIsInstance(member["last_seen"], float)

    def test_heartbeat_refreshes_last_seen_for_caller_only(self) -> None:
        """Test 2: heartbeat by A2 refreshes A2's last_seen only; info reflects it."""
        room = self._create_room(cap=5)
        self._join(room["room_id"], room["link_token"], "A1", self.token_a1)
        self._join(room["room_id"], room["link_token"], "A2", self.token_a2)
        self._join(room["room_id"], room["link_token"], "A3", self.token_a3)

        info_before = self._info(room["room_id"], "A1", self.token_a1)
        last_seen_before = {m["agent_id"]: m["last_seen"] for m in info_before["members"]}

        # Small delay so monotonic-clock last_seen actually advances.
        time.sleep(0.01)

        hb = self._heartbeat(room["room_id"], "A2", self.token_a2)
        self.assertEqual(hb["agent_id"], "A2")
        self.assertEqual(hb["status"], "active")
        self.assertIn("last_seen", hb)

        info_after = self._info(room["room_id"], "A1", self.token_a1)
        last_seen_after = {m["agent_id"]: m["last_seen"] for m in info_after["members"]}

        # A2's last_seen advanced.
        self.assertGreater(last_seen_after["A2"], last_seen_before["A2"])
        # A1 and A3 unchanged.
        self.assertEqual(last_seen_after["A1"], last_seen_before["A1"])
        self.assertEqual(last_seen_after["A3"], last_seen_before["A3"])

        # All still active.
        for m in info_after["members"]:
            self.assertEqual(m["status"], "active")

    def test_member_charge_leave_removes_member(self) -> None:
        """Test 3: A3 leaves; info member_count drops to 2; A3 absent."""
        room = self._create_room(cap=5)
        self._join(room["room_id"], room["link_token"], "A1", self.token_a1)
        self._join(room["room_id"], room["link_token"], "A2", self.token_a2)
        self._join(room["room_id"], room["link_token"], "A3", self.token_a3)

        leave_result = self._leave(room["room_id"], "A3", self.token_a3)
        self.assertEqual(leave_result["agent_id"], "A3")
        self.assertEqual(leave_result["status"], "left")

        info = self._info(room["room_id"], "A1", self.token_a1)
        self.assertEqual(info["member_count"], 2)
        member_ids = {m["agent_id"] for m in info["members"]}
        self.assertEqual(member_ids, {"A1", "A2"})
        self.assertNotIn("A3", member_ids)

    def test_rejoin_idempotent_no_overwrite(self) -> None:
        """Test 4: A3 re-joins with same agent_id + same actor token;
        info shows A3 active again (idempotent re-join, no overwrite)."""
        room = self._create_room(cap=5)
        self._join(room["room_id"], room["link_token"], "A1", self.token_a1)
        self._join(room["room_id"], room["link_token"], "A2", self.token_a2)
        self._join(room["room_id"], room["link_token"], "A3", self.token_a3,
                   capabilities=["read", "admin"])

        # A3 leaves.
        self._leave(room["room_id"], "A3", self.token_a3)
        info_after_leave = self._info(room["room_id"], "A1", self.token_a1)
        self.assertEqual(info_after_leave["member_count"], 2)

        # A3 re-joins with same token.
        rejoin_result = self._join(
            room["room_id"], room["link_token"], "A3", self.token_a3,
            capabilities=["read", "admin"],
        )
        self.assertEqual(rejoin_result["agent_id"], "A3")
        self.assertEqual(rejoin_result["status"], "active")

        info = self._info(room["room_id"], "A1", self.token_a1)
        self.assertEqual(info["member_count"], 3)
        members_by_id = {m["agent_id"]: m for m in info["members"]}
        self.assertIn("A3", members_by_id)
        self.assertEqual(members_by_id["A3"]["status"], "active")
        self.assertEqual(members_by_id["A3"]["capabilities"], ["read", "admin"])

    def test_room_tool_schemas_present_in_tools_list(self) -> None:
        """Test 5: tool schemas for the 4 room tools present in the TOOLS list."""
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        expected_tools = {
            "finalisma_room_create",
            "finalisma_room_join",
            "finalisma_room_info",
            "finalisma_room_heartbeat",
        }
        for tool_name in expected_tools:
            self.assertIn(tool_name, schemas, f"{tool_name} missing from TOOLS")

        # Spot-check required properties per ROOMS_DESIGN.md §8.
        create_props = schemas["finalisma_room_create"]["properties"]
        self.assertIn("team_id", create_props)
        self.assertIn("owner_agent_id", create_props)
        self.assertIn("cap", create_props)

        join_props = schemas["finalisma_room_join"]["properties"]
        self.assertIn("team_id", join_props)
        self.assertIn("room_id", join_props)
        self.assertIn("link_token", join_props)
        self.assertIn("agent_id", join_props)
        self.assertIn("consent", join_props)
        self.assertIn("actor_token", join_props)

        info_props = schemas["finalisma_room_info"]["properties"]
        self.assertIn("team_id", info_props)
        self.assertIn("room_id", info_props)
        self.assertIn("agent_id", info_props)
        self.assertIn("actor_token", info_props)

        hb_props = schemas["finalisma_room_heartbeat"]["properties"]
        self.assertIn("team_id", hb_props)
        self.assertIn("room_id", hb_props)
        self.assertIn("agent_id", hb_props)
        self.assertIn("actor_token", hb_props)


if __name__ == "__main__":
    unittest.main()
