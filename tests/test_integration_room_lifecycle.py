from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftError, WeftStore
from weft_mcp.server import WeftDispatcher, TOOLS


class RoomLifecycleIntegrationTests(unittest.TestCase):
    """RED deliverable — lifecycle contract for the 5 core room tools.

    Every room_* call MUST raise WeftError("unknown_tool", ...)
    until the tools are wired. This file asserts the full contract from
    docs/ROOMS_DESIGN.md §8/§9/§10 through the real MCP surface
    (WeftDispatcher + real WeftStore on temp SQLite).
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        # Register two agents and capture their actor tokens.
        reg_owner = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": "owner-1", "role": "architect"},
        )
        reg_member = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": "member-1", "role": "builder"},
        )
        reg_extra1 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": "member-2", "role": "builder"},
        )
        reg_extra2 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": "member-3", "role": "builder"},
        )
        self.owner_token = reg_owner["actor_token"]
        self.member_token = reg_member["actor_token"]
        self.extra1_token = reg_extra1["actor_token"]
        self.extra2_token = reg_extra2["actor_token"]

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    # -- 1. create returns room_id starting "room_", state "forming",
    #    link_token present, cap echo, expires_at present. Owner auto-joins. --
    def test_room_create_returns_forming_room_with_link_and_owner_is_member(self) -> None:
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        self.assertTrue(created["room_id"].startswith("room_"))
        self.assertEqual(created["state"], "forming")
        self.assertIn("link_token", created)
        self.assertTrue(len(created["link_token"]) > 0)
        self.assertEqual(created["cap"], 4)
        self.assertIn("expires_at", created)

        # Owner is already a member — info shows member_count 1 with owner present.
        info = self.dispatcher.call_tool(
            "room_info",
            {"team_id": "team-1", "room_id": created["room_id"], "agent_id": "owner-1", "actor_token": self.owner_token},
        )
        self.assertEqual(info["state"], "forming")
        self.assertEqual(info["cap"], 4)
        self.assertEqual(info["member_count"], 1)
        self.assertEqual(info["owner_agent_id"], "owner-1")
        member_ids = [m["agent_id"] for m in info["members"]]
        self.assertIn("owner-1", member_ids)

    # -- 2. join with consent=true admits agent 2; room transitions to active;
    #    info member_count 2. --
    def test_room_join_admits_second_agent_and_transitions_to_active(self) -> None:
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_id = created["room_id"]
        link_token = created["link_token"]

        joined = self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "link_token": link_token,
                "agent_id": "member-1",
                "consent": True,
                "actor_token": self.member_token,
            },
        )
        self.assertEqual(joined["room_id"], room_id)
        self.assertEqual(joined["agent_id"], "member-1")
        self.assertEqual(joined["status"], "active")
        self.assertEqual(joined["cursor"], 0)
        self.assertIn("joined_at", joined)

        info = self.dispatcher.call_tool(
            "room_info",
            {"team_id": "team-1", "room_id": room_id, "agent_id": "owner-1", "actor_token": self.owner_token},
        )
        self.assertEqual(info["state"], "active")
        self.assertEqual(info["member_count"], 2)

    # -- 3. join up to cap: cap=3, owner + 2 more = 3 total;
    #    4th join REFUSED with code "room_full". --
    def test_room_join_past_cap_is_refused_with_room_full(self) -> None:
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 3},
        )
        room_id = created["room_id"]
        link_token = created["link_token"]

        # Owner is member 1. Join member-1 (2) and member-2 (3) — cap reached.
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "link_token": link_token,
                "agent_id": "member-1",
                "consent": True,
                "actor_token": self.member_token,
            },
        )
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "link_token": link_token,
                "agent_id": "member-2",
                "consent": True,
                "actor_token": self.extra1_token,
            },
        )

        info = self.dispatcher.call_tool(
            "room_info",
            {"team_id": "team-1", "room_id": room_id, "agent_id": "owner-1", "actor_token": self.owner_token},
        )
        self.assertEqual(info["member_count"], 3)

        # 4th join must be refused with code "room_full".
        with self.assertRaises(WeftError) as refused:
            self.dispatcher.call_tool(
                "room_join",
                {
                    "team_id": "team-1",
                    "room_id": room_id,
                    "link_token": link_token,
                    "agent_id": "member-3",
                    "consent": True,
                    "actor_token": self.extra2_token,
                },
            )
        self.assertEqual(refused.exception.code, "room_full")

    # -- 4. leave: member leaves, info member_count drops, leave returns
    #    status "left"; re-join with same agent_id+same actor token reactivates
    #    (idempotent re-join, no overwrite). --
    def test_room_leave_then_idempotent_rejoin(self) -> None:
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_id = created["room_id"]
        link_token = created["link_token"]

        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "link_token": link_token,
                "agent_id": "member-1",
                "consent": True,
                "actor_token": self.member_token,
            },
        )
        info_after_join = self.dispatcher.call_tool(
            "room_info",
            {"team_id": "team-1", "room_id": room_id, "agent_id": "owner-1", "actor_token": self.owner_token},
        )
        self.assertEqual(info_after_join["member_count"], 2)

        # member-1 leaves.
        left = self.dispatcher.call_tool(
            "room_leave",
            {"team_id": "team-1", "room_id": room_id, "agent_id": "member-1", "actor_token": self.member_token},
        )
        self.assertEqual(left["room_id"], room_id)
        self.assertEqual(left["agent_id"], "member-1")
        self.assertEqual(left["status"], "left")

        info_after_leave = self.dispatcher.call_tool(
            "room_info",
            {"team_id": "team-1", "room_id": room_id, "agent_id": "owner-1", "actor_token": self.owner_token},
        )
        self.assertEqual(info_after_leave["member_count"], 1)

        # Re-join with same agent_id + same actor token → idempotent reactivation.
        rejoined = self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "link_token": link_token,
                "agent_id": "member-1",
                "consent": True,
                "actor_token": self.member_token,
            },
        )
        self.assertEqual(rejoined["agent_id"], "member-1")
        self.assertEqual(rejoined["status"], "active")

        info_after_rejoin = self.dispatcher.call_tool(
            "room_info",
            {"team_id": "team-1", "room_id": room_id, "agent_id": "owner-1", "actor_token": self.owner_token},
        )
        self.assertEqual(info_after_rejoin["member_count"], 2)

    # -- 5. close by owner: state "closed"; join after close REFUSED with
    #    "room_closed"; info still readable by a member with state closed. --
    def test_room_close_by_owner_blocks_joins_and_preserves_readable_state(self) -> None:
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_id = created["room_id"]
        link_token = created["link_token"]

        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "link_token": link_token,
                "agent_id": "member-1",
                "consent": True,
                "actor_token": self.member_token,
            },
        )

        closed = self.dispatcher.call_tool(
            "room_close",
            {"team_id": "team-1", "room_id": room_id, "owner_agent_id": "owner-1", "actor_token": self.owner_token},
        )
        self.assertEqual(closed["room_id"], room_id)
        self.assertEqual(closed["state"], "closed")

        # Info still readable by a member — state is "closed".
        info = self.dispatcher.call_tool(
            "room_info",
            {"team_id": "team-1", "room_id": room_id, "agent_id": "member-1", "actor_token": self.member_token},
        )
        self.assertEqual(info["state"], "closed")

        # Join after close REFUSED with "room_closed".
        with self.assertRaises(WeftError) as refused:
            self.dispatcher.call_tool(
                "room_join",
                {
                    "team_id": "team-1",
                    "room_id": room_id,
                    "link_token": link_token,
                    "agent_id": "member-2",
                    "consent": True,
                    "actor_token": self.extra1_token,
                },
            )
        self.assertEqual(refused.exception.code, "room_closed")

    # -- 6. close by NON-owner refused (assert error code). --
    def test_room_close_by_non_owner_is_refused(self) -> None:
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_id = created["room_id"]
        link_token = created["link_token"]

        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "link_token": link_token,
                "agent_id": "member-1",
                "consent": True,
                "actor_token": self.member_token,
            },
        )

        with self.assertRaises(WeftError) as refused:
            self.dispatcher.call_tool(
                "room_close",
                {"team_id": "team-1", "room_id": room_id, "owner_agent_id": "member-1", "actor_token": self.member_token},
            )
        # Design doc §9 #11: non-owner close refused — code is "member_required"
        # (or a new "owner_required"); accept either.
        self.assertIn(refused.exception.code, ("member_required", "owner_required"))

    # -- 7. tool schemas for the 5 core tools present in the TOOLS list. --
    def test_room_tool_schemas_are_registered(self) -> None:
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        expected_tools = {
            "room_create",
            "room_join",
            "room_info",
            "room_leave",
            "room_close",
        }
        for tool_name in expected_tools:
            self.assertIn(tool_name, schemas, f"{tool_name} not registered in TOOLS")
            schema = schemas[tool_name]
            self.assertIn("properties", schema)
            self.assertIn("type", schema)
            self.assertEqual(schema["type"], "object")

        # Spot-check required-arg shapes per §8.
        create_props = schemas["room_create"]["properties"]
        self.assertIn("team_id", create_props)
        self.assertIn("owner_agent_id", create_props)
        self.assertIn("cap", create_props)

        join_props = schemas["room_join"]["properties"]
        self.assertIn("team_id", join_props)
        self.assertIn("room_id", join_props)
        self.assertIn("link_token", join_props)
        self.assertIn("agent_id", join_props)
        self.assertIn("consent", join_props)
        self.assertIn("actor_token", join_props)

        info_props = schemas["room_info"]["properties"]
        self.assertIn("team_id", info_props)
        self.assertIn("room_id", info_props)
        self.assertIn("agent_id", info_props)
        self.assertIn("actor_token", info_props)

        leave_props = schemas["room_leave"]["properties"]
        self.assertIn("team_id", leave_props)
        self.assertIn("room_id", leave_props)
        self.assertIn("agent_id", leave_props)
        self.assertIn("actor_token", leave_props)

        close_props = schemas["room_close"]["properties"]
        self.assertIn("team_id", close_props)
        self.assertIn("room_id", close_props)
        self.assertIn("owner_agent_id", close_props)
        self.assertIn("actor_token", close_props)


if __name__ == "__main__":
    unittest.main()
