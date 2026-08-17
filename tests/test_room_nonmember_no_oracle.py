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
from weft_mcp.room import RoomError

# A wrong-but-well-formed actor token (>= 16 chars so it reaches the stored
# credential comparison and fails with actor_auth_invalid).
BAD_TOKEN = "fst_actor_bogus_token_value_which_is_long"

# Member-only room tools exercised by this suite (the full surface).
ROOM_TOOLS = [
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

FAKE_ROOM_ID = "room_nonexistent_fake_0000000000"

# Dispatcher tool name -> RoomStore method, for direct store-level probes.
_STORE_METHOD = {
    "room_info": "room_info",
    "room_leave": "leave_room",
    "room_close": "close_room",
    "room_revoke_link": "revoke_link",
    "room_send": "room_send",
    "room_poll": "poll",
    "room_ack": "ack",
    "room_heartbeat": "heartbeat",
    "room_groups": "groups",
    "room_receipts": "receipts",
}


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
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 8,
             "actor_token": self.owner},
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

    def _outsider_args(self, tool: str, room_id: str, token: str | None = None) -> dict:
        """Arguments for a NON-member probing ``room_id`` with ``token`` (default valid outsider token)."""
        if token is None:
            token = self.outsider
        common = {"team_id": "team-1", "room_id": room_id}
        if tool == "room_send":
            return {
                **common,
                "sender_agent_id": "outsider-3",
                "target_spec": "*",
                "payload": {"text": "probe"},
                "actor_token": token,
            }
        if tool == "room_close":
            return {**common, "owner_agent_id": "outsider-3", "actor_token": token}
        if tool == "room_revoke_link":
            return {
                **common,
                "owner_agent_id": "outsider-3",
                "link_id": self.link_id,
                "actor_token": token,
            }
        if tool == "room_ack":
            return {**common, "agent_id": "outsider-3", "seq": 0, "actor_token": token}
        if tool == "room_groups":
            return {
                **common,
                "agent_id": "outsider-3",
                "group_name": "g",
                "action": "list",
                "actor_token": token,
            }
        if tool == "room_receipts":
            return {
                **common,
                "agent_id": "outsider-3",
                "entry_ids": [],
                "actor_token": token,
            }
        return {**common, "agent_id": "outsider-3", "actor_token": token}

    def _store_error_code(self, tool: str, room_id: str, token: str | None) -> str:
        """Call the RoomStore method directly (bypasses the dispatcher arg gate)."""
        method = getattr(self.dispatcher.rooms, _STORE_METHOD[tool])
        args: dict = {"team_id": "team-1", "room_id": room_id, "actor_token": token}
        if tool == "room_send":
            args.update(sender_agent_id="outsider-3", target_spec="*", payload={"text": "probe"})
        elif tool == "room_close":
            args["caller_agent_id"] = "outsider-3"
        elif tool == "room_revoke_link":
            args.update(owner_agent_id="outsider-3", link_id=self.link_id)
        elif tool == "room_ack":
            args.update(agent_id="outsider-3", seq=0)
        elif tool == "room_groups":
            args.update(agent_id="outsider-3", group_name="g", action="list")
        elif tool == "room_receipts":
            args.update(agent_id="outsider-3", entry_ids=[])
        else:
            args["agent_id"] = "outsider-3"
        with self.assertRaises(RoomError) as ctx:
            method(**args)
        return ctx.exception.code

    # ------------------------------------------------------------------
    # Every room tool: a BAD token gives the IDENTICAL error for a real
    # room and a fabricated one — actor_auth_invalid, no existence oracle.
    # ------------------------------------------------------------------
    def test_bad_token_error_is_identical_real_vs_fake_for_all_room_tools(self) -> None:
        for tool in ROOM_TOOLS:
            with self.subTest(tool=tool):
                real = self._error_code(tool, self._outsider_args(tool, self.room_id, BAD_TOKEN))
                fake = self._error_code(tool, self._outsider_args(tool, FAKE_ROOM_ID, BAD_TOKEN))
                self.assertEqual(
                    real,
                    fake,
                    f"{tool}: bad-token caller must get the SAME error for a real and a fake room",
                )
                self.assertEqual(real, "actor_auth_invalid")

    # ------------------------------------------------------------------
    # Every room tool: NO token at the store layer gives the IDENTICAL
    # error for a real room and a fabricated one. The dispatcher gates the
    # missing argument before any DB work (invalid_argument), so this probes
    # the layer underneath — where the ordering bug actually lived.
    # ------------------------------------------------------------------
    def test_no_token_store_error_is_identical_real_vs_fake_for_all_room_tools(self) -> None:
        for tool in ROOM_TOOLS:
            with self.subTest(tool=tool):
                real = self._store_error_code(tool, self.room_id, None)
                fake = self._store_error_code(tool, FAKE_ROOM_ID, None)
                self.assertEqual(
                    real,
                    fake,
                    f"{tool}: no-token caller must get the SAME error for a real and a fake room",
                )
                self.assertEqual(real, "actor_auth_invalid")

    # ------------------------------------------------------------------
    # Every room tool: NO token through the real dispatcher is gated on the
    # missing required argument BEFORE any room/auth lookup — identical for
    # a real and a fake room by construction.
    # ------------------------------------------------------------------
    def test_no_token_dispatcher_is_gated_identical_for_all_room_tools(self) -> None:
        for tool in ROOM_TOOLS:
            with self.subTest(tool=tool):
                args = self._outsider_args(tool, self.room_id)
                args.pop("actor_token")
                real = self._error_code(tool, args)
                args = self._outsider_args(tool, FAKE_ROOM_ID)
                args.pop("actor_token")
                fake = self._error_code(tool, args)
                self.assertEqual(
                    real,
                    fake,
                    f"{tool}: no-token dispatcher call must not distinguish real from fake room",
                )
                self.assertEqual(real, "invalid_argument")

    # ------------------------------------------------------------------
    # Every room tool: non-member gets the IDENTICAL error for a real room
    # and a fabricated one — room_not_found, no existence oracle.
    # ------------------------------------------------------------------
    def test_non_member_error_is_identical_real_vs_fake_for_all_room_tools(self) -> None:
        for tool in ROOM_TOOLS:
            with self.subTest(tool=tool):
                real = self._error_code(tool, self._outsider_args(tool, self.room_id))
                fake = self._error_code(tool, self._outsider_args(tool, FAKE_ROOM_ID))
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

    def test_join_short_token_is_a_normal_invalid_link_error(self) -> None:
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool("room_join", {
                "team_id": "team-1",
                "room_id": self.room_id,
                "link_token": "short",
                "agent_id": "outsider-3",
                "consent": True,
                "actor_token": self.outsider,
            })
        self.assertEqual(ctx.exception.code, "invalid_link")

    def test_join_bad_actor_does_not_reveal_room_existence(self) -> None:
        def code(room_id: str) -> str:
            with self.assertRaises(WeftError) as ctx:
                self.dispatcher.call_tool("room_join", {
                    "team_id": "team-1",
                    "room_id": room_id,
                    "link_token": self.link_token,
                    "agent_id": "outsider-3",
                    "consent": True,
                    "actor_token": BAD_TOKEN,
                })
            return ctx.exception.code

        real = code(self.room_id)
        fake = code(FAKE_ROOM_ID)
        self.assertEqual(real, fake)
        self.assertEqual(real, "actor_auth_invalid")

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
                {"team_id": "team-1", "room_id": FAKE_ROOM_ID,
                 "agent_id": "member-2", "actor_token": self.member},
            )
        self.assertEqual(ctx.exception.code, "room_not_found")


if __name__ == "__main__":
    unittest.main()
