"""Regression tests for coordinator room/team binding.

Room membership is keyed by ``room_id`` and ``agent_id`` while actor
credentials are keyed by ``(team_id, agent_id)``. Every room operation must
bind those two namespaces to the room's stored team, otherwise reusing an
agent id in another team can turn a room id into a cross-team handle.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftError, WeftStore
from weft_mcp.server import WeftDispatcher


class RoomTeamIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)

        self.owner_token = self._register("team-a", "owner-a")
        self.shared_a_token = self._register("team-a", "shared")
        # The same agent id is valid in a separate team, but its credential
        # must not authorize access to team-a's room.
        self.shared_b_token = self._register("team-b", "shared")
        self.joiner_b_token = self._register("team-b", "joiner")

        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-a", "owner_agent_id": "owner-a", "cap": 5},
        )
        self.room_id = created["room_id"]
        self.link_token = created["link_token"]
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-a",
                "room_id": self.room_id,
                "link_token": self.link_token,
                "agent_id": "shared",
                "consent": True,
                "actor_token": self.shared_a_token,
            },
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _register(self, team_id: str, agent_id: str) -> str:
        result = self.dispatcher.call_tool(
            "register_agent", {"team_id": team_id, "agent_id": agent_id},
        )
        return result["actor_token"]

    def test_member_id_reused_in_another_team_cannot_read_room(self) -> None:
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_info",
                {
                    "team_id": "team-b",
                    "room_id": self.room_id,
                    "agent_id": "shared",
                    "actor_token": self.shared_b_token,
                },
            )
        self.assertEqual(ctx.exception.code, "room_not_found")

    def test_valid_link_cannot_join_room_from_another_team(self) -> None:
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_join",
                {
                    "team_id": "team-b",
                    "room_id": self.room_id,
                    "link_token": self.link_token,
                    "agent_id": "joiner",
                    "consent": True,
                    "actor_token": self.joiner_b_token,
                },
            )
        self.assertEqual(ctx.exception.code, "room_not_found")

        info = self.dispatcher.call_tool(
            "room_info",
            {
                "team_id": "team-a",
                "room_id": self.room_id,
                "agent_id": "owner-a",
                "actor_token": self.owner_token,
            },
        )
        self.assertEqual(info["member_count"], 2,
                         "a rejected cross-team join must not mutate membership")


if __name__ == "__main__":
    unittest.main()
