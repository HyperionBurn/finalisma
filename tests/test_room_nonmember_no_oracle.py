"""RED deliverable — no room-existence oracle for non-members.

A caller who is not a member of a room must not be able to learn whether the
room exists. For every room_* tool, a valid-token NON-member gets the identical
error for a real room and a fabricated one — `room_not_found` — matching the
hosted cloud plane's no-oracle 404. Members remain entitled to precise errors
(`owner_required`, `member_required` for a target, and so on).

The assertions compare the real-vs-fake responses to EACH OTHER first, so a
future change that re-splits the codes into two NEW distinct values still fails
this suite even if both differ from today's literal.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftError, WeftStore
from weft_mcp.server import WeftDispatcher


class RoomNonMemberNoOracleTests(unittest.TestCase):
    """Non-members get a uniform room_not_found; members keep precise errors."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        self.owner = self._register("owner-1")
        self.member = self._register("member-2")
        self.outsider = self._register("outsider-3")
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 8},
        )
        self.room_id = created["room_id"]
        self.link_token = created["link_token"]
        self.link_id = created["link_id"]
        # member-2 joins; outsider-3 never does.
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": self.room_id,
                "link_token": self.link_token,
                "agent_id": "member-2",
                "consent": True,
                "actor_token": self.member,
            },
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _register(self, agent_id: str) -> str:
        result = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": agent_id},
        )
        return result["actor_token"]

    def _error_code(self, tool: str, args: dict) -> str:
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(tool, args)
        return ctx.exception.code

    def _outsider_args(self, tool: str, room_id: str) -> dict:
        """Arguments for a valid-token NON-member probing ``room_id``."""
        common = {"team_id": "team-1", "room_id": room_id}
        if tool == "room_send":
            return {
                **common,
                "sender_agent_id": "outsider-3",
                "target_spec": "*",
                "payload": {"text": "probe"},
                "actor_token": self.outsider,
            }
        if tool == "room_close":
            return {**common, "owner_agent_id": "outsider-3", "actor_token": self.outsider}
        if tool == "room_revoke_link":
            return {
                **common,
                "owner_agent_id": "outsider-3",
                "link_id": self.link_id,
                "actor_token": self.outsider,
            }
        if tool == "room_ack":
            return {**common, "agent_id": "outsider-3", "seq": 0, "actor_token": self.outsider}
        if tool == "room_groups":
            return {
                **common,
                "agent_id": "outsider-3",
                "group_name": "g",
                "action": "list",
                "actor_token": self.outsider,
            }
        if tool == "room_receipts":
            return {
                **common,
                "agent_id": "outsider-3",
                "entry_ids": [],
                "actor_token": self.outsider,
            }
        return {**common, "agent_id": "outsider-3", "actor_token": self.outsider}

    # ------------------------------------------------------------------
    # Every room tool: non-member gets the IDENTICAL error for a real room
    # and a fabricated one — room_not_found, no existence oracle.
    # ------------------------------------------------------------------
    def test_non_member_error_is_identical_real_vs_fake_for_all_room_tools(self) -> None:
        tools = [
            "room_info",
            "room_send",
            "room_poll",
            "room_ack",
            "room_heartbeat",
            "room_groups",
            "room_receipts",
            "room_leave",
            "room_close",
            "room_revoke_link",
        ]
        fake_room_id = "room_nonexistent_fake_0000000000"
        for tool in tools:
            with self.subTest(tool=tool):
                real = self._error_code(tool, self._outsider_args(tool, self.room_id))
                fake = self._error_code(tool, self._outsider_args(tool, fake_room_id))
                self.assertEqual(
                    real,
                    fake,
                    f"{tool}: non-member must get the SAME error for a real and a fake room",
                )
                self.assertEqual(real, "room_not_found")

    # ------------------------------------------------------------------
    # A genuine member's normal operations are unaffected.
    # ------------------------------------------------------------------
    def test_member_happy_path_unaffected(self) -> None:
        info = self.dispatcher.call_tool(
            "room_info",
            {"team_id": "team-1", "room_id": self.room_id, "agent_id": "member-2",
             "actor_token": self.member},
        )
        self.assertEqual(info["room_id"], self.room_id)
        self.assertEqual(info["state"], "active")

        hb = self.dispatcher.call_tool(
            "room_heartbeat",
            {"team_id": "team-1", "room_id": self.room_id, "agent_id": "member-2",
             "actor_token": self.member},
        )
        self.assertEqual(hb["status"], "active")

        sent = self.dispatcher.call_tool(
            "room_send",
            {"team_id": "team-1", "room_id": self.room_id, "sender_agent_id": "member-2",
             "target_spec": "*", "payload": {"text": "hi"}, "actor_token": self.member},
        )
        self.assertEqual(sent["room_id"], self.room_id)
        entry_ids = [r["entry_id"] for r in sent["receipts"]]
        self.assertEqual(len(entry_ids), 1)

        polled = self.dispatcher.call_tool(
            "room_poll",
            {"team_id": "team-1", "room_id": self.room_id, "agent_id": "member-2",
             "actor_token": self.member},
        )
        self.assertGreaterEqual(len(polled["events"]), 1)
        head = polled["cursor_head"]

        acked = self.dispatcher.call_tool(
            "room_ack",
            {"team_id": "team-1", "room_id": self.room_id, "agent_id": "member-2",
             "seq": head, "actor_token": self.member},
        )
        self.assertEqual(acked["last_ack_seq"], head)

        groups = self.dispatcher.call_tool(
            "room_groups",
            {"team_id": "team-1", "room_id": self.room_id, "agent_id": "member-2",
             "group_name": "reviewers", "action": "add", "members": ["member-2"],
             "actor_token": self.member},
        )
        self.assertIn("member-2", groups["members"])

        receipts = self.dispatcher.call_tool(
            "room_receipts",
            {"team_id": "team-1", "room_id": self.room_id, "agent_id": "member-2",
             "entry_ids": entry_ids, "actor_token": self.member},
        )
        self.assertEqual(len(receipts["receipts"]), 1)

    # ------------------------------------------------------------------
    # Members keep precise errors.
    # ------------------------------------------------------------------
    def test_member_keeps_precise_errors(self) -> None:
        # A member (not owner) closing is refused with the precise owner_required.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_close",
                {"team_id": "team-1", "room_id": self.room_id, "owner_agent_id": "member-2",
                 "actor_token": self.member},
            )
        self.assertEqual(ctx.exception.code, "owner_required")

        # A member revoking a link is refused with the precise owner_required.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_revoke_link",
                {"team_id": "team-1", "room_id": self.room_id, "owner_agent_id": "member-2",
                 "link_id": self.link_id, "actor_token": self.member},
            )
        self.assertEqual(ctx.exception.code, "owner_required")

        # A member adding a non-member TARGET to a group keeps the precise
        # member_required for the target (reachable only by entitled callers).
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_groups",
                {"team_id": "team-1", "room_id": self.room_id, "agent_id": "member-2",
                 "group_name": "g", "action": "add", "members": ["outsider-3"],
                 "actor_token": self.member},
            )
        self.assertEqual(ctx.exception.code, "member_required")

        # A member who mistypes a room id still gets the precise room_not_found.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_info",
                {"team_id": "team-1", "room_id": "room_nonexistent_fake_0000000000",
                 "agent_id": "member-2", "actor_token": self.member},
            )
        self.assertEqual(ctx.exception.code, "room_not_found")


if __name__ == "__main__":
    unittest.main()
