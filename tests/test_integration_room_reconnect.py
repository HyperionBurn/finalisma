"""Integration test: ordered replay + per-member cursor + reconnect contract.

RED deliverable for the room tools. Drives the real MCP surface (WeftDispatcher)
with a real SQLite store (require_actor_auth=True). Every room_* call here
MUST fail with WeftError("unknown_tool", ...) until the room tools are wired.

Contracts asserted from docs/ROOMS_DESIGN.md §5 and §8:
  - room_event_log: ordered, append-only, UNIQUE(room_id, seq).
  - room_cursors: per-member (room_id, agent_id) PK; monotonic MAX ack.
  - room_poll(after_seq=N) returns events with seq > N, no loss, no duplicates.
  - Reconnect resumes from last_ack_seq with full ordered replay.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftStore
from weft_mcp.server import WeftDispatcher, TOOLS


class RoomReconnectIntegrationTests(unittest.TestCase):
    """Ordered-event-log + per-member-cursor + reconnect contract (RED)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)

        # Register three agents under one team.
        reg_a1 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": "agent-1", "role": "owner"},
        )
        reg_a2 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": "agent-2", "role": "member"},
        )
        reg_a3 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": "agent-3", "role": "member"},
        )
        self.token_a1 = reg_a1["actor_token"]
        self.token_a2 = reg_a2["actor_token"]
        self.token_a3 = reg_a3["actor_token"]

        # Create room (owner = agent-1), cap=5.
        created = self.dispatcher.call_tool(
            "room_create",
            {
                "team_id": "team-1",
                "owner_agent_id": "agent-1",
                "cap": 5,
                "actor_token": self.token_a1,
            },
        )
        self.room_id = created["room_id"]
        self.link_token = created["link_token"]

        # Owner (agent-1) joins.
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": self.room_id,
                "link_token": self.link_token,
                "agent_id": "agent-1",
                "consent": True,
                "actor_token": self.token_a1,
            },
        )

        # Join agent-2 and agent-3.
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": self.room_id,
                "link_token": self.link_token,
                "agent_id": "agent-2",
                "consent": True,
                "actor_token": self.token_a2,
            },
        )
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": self.room_id,
                "link_token": self.link_token,
                "agent_id": "agent-3",
                "consent": True,
                "actor_token": self.token_a3,
            },
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    # --- helpers -----------------------------------------------------------

    def _poll(self, agent_id: str, actor_token: str, after_seq: int | None = None, limit: int | None = None) -> dict:
        args = {
            "team_id": "team-1",
            "room_id": self.room_id,
            "agent_id": agent_id,
            "actor_token": actor_token,
        }
        if after_seq is not None:
            args["after_seq"] = after_seq
        if limit is not None:
            args["limit"] = limit
        return self.dispatcher.call_tool("room_poll", args)

    def _ack(self, agent_id: str, actor_token: str, seq: int) -> dict:
        return self.dispatcher.call_tool(
            "room_ack",
            {
                "team_id": "team-1",
                "room_id": self.room_id,
                "agent_id": agent_id,
                "seq": seq,
                "actor_token": actor_token,
            },
        )

    def _send(self, sender_agent_id: str, actor_token: str, target_spec, payload: dict) -> dict:
        return self.dispatcher.call_tool(
            "room_send",
            {
                "team_id": "team-1",
                "room_id": self.room_id,
                "sender_agent_id": sender_agent_id,
                "target_spec": target_spec,
                "payload": payload,
                "actor_token": actor_token,
            },
        )

    # --- tests -------------------------------------------------------------

    def test_01_event_log_is_ordered_and_append_only(self) -> None:
        """After create + 3 joins, agent-3 poll(after_seq=0) returns strictly
        increasing seq, including room.created and each room.joined."""
        result = self._poll("agent-3", self.token_a3, after_seq=0)
        events = result["events"]
        self.assertIsInstance(events, list)
        self.assertGreaterEqual(len(events), 4, "expected room.created + 3 joins at minimum")

        # Strictly increasing seq.
        seqs = [e["seq"] for e in events]
        for prev, cur in zip(seqs, seqs[1:]):
            self.assertLess(prev, cur, f"seq not strictly increasing: {seqs}")

        # Event shape.
        for e in events:
            self.assertIn("event_id", e)
            self.assertIn("seq", e)
            self.assertIn("origin_agent", e)
            self.assertIn("kind", e)
            self.assertIn("payload", e)
            self.assertIn("created_at", e)

        kinds = {e["kind"] for e in events}
        self.assertIn("room.created", kinds)
        joined_events = [e for e in events if e["kind"] == "room.joined"]
        self.assertGreaterEqual(len(joined_events), 3, "expected 3 room.joined events")

        # next_seq points past the last returned event.
        self.assertEqual(result["next_seq"], seqs[-1] + 1)
        self.assertFalse(result["has_more"])

    def test_02_room_send_emits_room_message_with_increasing_seq(self) -> None:
        """Sending a message appends a room.message event with a new seq."""
        before = self._poll("agent-1", self.token_a1, after_seq=0)
        last_seq_before = before["events"][-1]["seq"]

        sent = self._send(
            "agent-1",
            self.token_a1,
            target_spec="*",
            payload={"text": "hello room"},
        )
        self.assertIn("envelope", sent)
        self.assertIn("receipts", sent)
        self.assertIn("seq", sent)
        self.assertGreater(sent["seq"], last_seq_before)

        after = self._poll("agent-1", self.token_a1, after_seq=last_seq_before)
        new_events = after["events"]
        self.assertEqual(len(new_events), 1)
        self.assertEqual(new_events[0]["kind"], "room.message")
        self.assertEqual(new_events[0]["seq"], sent["seq"])
        self.assertEqual(new_events[0]["origin_agent"], "agent-1")

    def test_03_per_member_cursor_independence(self) -> None:
        """agent-2 acks seq=K; agent-3 does NOT ack. agent-3's poll still
        returns all events (last_ack_seq stays 0). agent-2's poll(after_seq=K)
        returns only events after K (no duplicates)."""
        all_events = self._poll("agent-3", self.token_a3, after_seq=0)["events"]
        self.assertGreaterEqual(len(all_events), 4)
        k = all_events[2]["seq"]  # ack up to the 3rd event for agent-2

        ack_res = self._ack("agent-2", self.token_a2, k)
        self.assertEqual(ack_res["last_ack_seq"], k)

        # agent-3 has not acked: last_ack_seq stays 0, full log returned.
        a3_poll = self._poll("agent-3", self.token_a3, after_seq=0)
        self.assertEqual(a3_poll["last_ack_seq"], 0)
        self.assertEqual(len(a3_poll["events"]), len(all_events))

        # agent-2 polls from K onward: only events after K.
        a2_poll = self._poll("agent-2", self.token_a2, after_seq=k)
        self.assertEqual(a2_poll["last_ack_seq"], k)
        for e in a2_poll["events"]:
            self.assertGreater(e["seq"], k)

        # No duplicates: a seq already acked must not reappear.
        returned_seqs = {e["seq"] for e in a2_poll["events"]}
        self.assertTrue(returned_seqs.isdisjoint(set(range(1, k + 1))))

    def test_04_monotonic_ack_max_semantics(self) -> None:
        """ack high then ack a lower seq leaves last_ack_seq at the high value
        (MAX). poll after the high seq returns nothing new."""
        # First send a message so we have enough events.
        self._send("agent-1", self.token_a1, target_spec="*", payload={"text": "m1"})
        self._send("agent-1", self.token_a1, target_spec="*", payload={"text": "m2"})

        all_events = self._poll("agent-1", self.token_a1, after_seq=0)["events"]
        self.assertGreaterEqual(len(all_events), 5)
        high_seq = all_events[-1]["seq"]  # ack the LAST event — nothing newer follows

        ack_high = self._ack("agent-1", self.token_a1, high_seq)
        self.assertEqual(ack_high["last_ack_seq"], high_seq)

        # Ack a lower seq — must not move cursor backward.
        ack_low = self._ack("agent-1", self.token_a1, 3)
        self.assertEqual(ack_low["last_ack_seq"], high_seq)

        # Poll from the high seq returns nothing new.
        after = self._poll("agent-1", self.token_a1, after_seq=high_seq)
        self.assertEqual(after["events"], [])
        self.assertFalse(after["has_more"])

    def test_05_no_loss_reconnect(self) -> None:
        """agent-2 polls from after_seq=0 (fresh cursor after a disconnect),
        gets the FULL ordered log with no gaps; polling again from next_seq
        returns empty (no duplicates)."""
        # Generate some traffic.
        self._send("agent-1", self.token_a1, target_spec="*", payload={"text": "hi"})
        self._send("agent-3", self.token_a3, target_spec="*", payload={"text": "yo"})

        # Fresh reconnect: poll from 0.
        first = self._poll("agent-2", self.token_a2, after_seq=0)
        events = first["events"]
        self.assertGreaterEqual(len(events), 6)  # created + 3 joins + 2 messages

        # No gaps: seqs are consecutive integers starting at 1.
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, list(range(1, len(events) + 1)),
                         f"gaps in replay: {seqs}")

        # Ordered.
        for prev, cur in zip(seqs, seqs[1:]):
            self.assertLess(prev, cur)

        # Second poll from next_seq returns empty — no duplicates.
        second = self._poll("agent-2", self.token_a2, after_seq=first["next_seq"])
        self.assertEqual(second["events"], [])
        self.assertFalse(second["has_more"])
        self.assertEqual(second["next_seq"], first["next_seq"])

    def test_06_tool_schemas_present(self) -> None:
        """The 5 core room tools exist in the TOOLS list with schemas."""
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        expected = {
            "room_create",
            "room_join",
            "room_poll",
            "room_ack",
            "room_send",
        }
        for tool_name in expected:
            self.assertIn(tool_name, schemas, f"missing schema for {tool_name}")
            props = schemas[tool_name]["properties"]
            self.assertIsInstance(props, dict)
            self.assertTrue(len(props) > 0, f"{tool_name} has empty properties")

        # Spot-check required fields per ROOMS_DESIGN.md §8.
        self.assertIn("team_id", schemas["room_create"]["properties"])
        self.assertIn("owner_agent_id", schemas["room_create"]["properties"])
        self.assertIn("cap", schemas["room_create"]["properties"])

        self.assertIn("room_id", schemas["room_join"]["properties"])
        self.assertIn("link_token", schemas["room_join"]["properties"])
        self.assertIn("consent", schemas["room_join"]["properties"])

        self.assertIn("actor_token", schemas["room_poll"]["properties"])
        self.assertIn("actor_token", schemas["room_ack"]["properties"])
        self.assertIn("actor_token", schemas["room_send"]["properties"])


if __name__ == "__main__":
    unittest.main()
