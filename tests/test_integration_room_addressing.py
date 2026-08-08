"""Integration tests for the Room addressing + delivery-receipt contract.

RED deliverable — the room_* tools are not wired yet. Every
room_* call_tool MUST raise WeftError("unknown_tool", ...).
These tests assert the full addressing contract through the real MCP
surface (WeftDispatcher.call_tool) against a real store with
require_actor_auth=True. No mocks.

Authoritative spec: docs/ROOMS_DESIGN.md §6 (addressing + receipts),
§8 (tool surface), §9 (negative cases).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftError, WeftStore
from weft_mcp.server import WeftDispatcher, TOOLS


TEAM_ID = "wave-e"
OWNER_ID = "agent-a1"  # A1 — room owner
AGENT_A2 = "agent-a2"
AGENT_A3 = "agent-a3"
ROOM_CAP = 5


class RoomAddressingIntegrationTests(unittest.TestCase):
    """Addressing + delivery-receipt contract through the real MCP surface."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)

        # Register three agents and capture their actor tokens.
        reg_a1 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": TEAM_ID, "agent_id": OWNER_ID, "role": "owner"},
        )
        reg_a2 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": TEAM_ID, "agent_id": AGENT_A2, "role": "reviewer"},
        )
        reg_a3 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": TEAM_ID, "agent_id": AGENT_A3, "role": "reviewer"},
        )
        self.token_a1 = reg_a1["actor_token"]
        self.token_a2 = reg_a2["actor_token"]
        self.token_a3 = reg_a3["actor_token"]

        # Create a room (owner A1) and join A2, A3.
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": TEAM_ID, "owner_agent_id": OWNER_ID, "cap": ROOM_CAP},
        )
        self.room_id = created["room_id"]
        self.link_token = created["link_token"]

        # Owner A1 joins (idempotent — owner may already be a member).
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": TEAM_ID,
                "room_id": self.room_id,
                "link_token": self.link_token,
                "agent_id": OWNER_ID,
                "consent": True,
                "actor_token": self.token_a1,
            },
        )
        # A2 joins.
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": TEAM_ID,
                "room_id": self.room_id,
                "link_token": self.link_token,
                "agent_id": AGENT_A2,
                "consent": True,
                "actor_token": self.token_a2,
            },
        )
        # A3 joins.
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": TEAM_ID,
                "room_id": self.room_id,
                "link_token": self.link_token,
                "agent_id": AGENT_A3,
                "consent": True,
                "actor_token": self.token_a3,
            },
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------
    #  Schema-assert pattern (mirrors test_actor_credentials_server.py)
    # ------------------------------------------------------------------
    def test_tool_schemas_present_in_TOOLS_list(self) -> None:
        """The 5 room-addressing tools must be declared in the TOOLS list."""
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        expected_room_tools = {
            "room_create",
            "room_join",
            "room_groups",
            "room_send",
            "room_receipts",
        }
        for tool_name in expected_room_tools:
            self.assertIn(tool_name, schemas, f"schema missing: {tool_name}")

        # room_create: team_id, owner_agent_id, cap (required)
        create_props = schemas["room_create"]["properties"]
        for key in ("team_id", "owner_agent_id", "cap"):
            self.assertIn(key, create_props)

        # room_join: team_id, room_id, link_token, agent_id, consent, actor_token
        join_props = schemas["room_join"]["properties"]
        for key in ("team_id", "room_id", "link_token", "agent_id", "consent", "actor_token"):
            self.assertIn(key, join_props)

        # room_groups: team_id, room_id, agent_id, group_name, action, actor_token
        groups_props = schemas["room_groups"]["properties"]
        for key in ("team_id", "room_id", "agent_id", "group_name", "action", "actor_token"):
            self.assertIn(key, groups_props)

        # room_send: team_id, room_id, sender_agent_id, target_spec, payload, actor_token
        send_props = schemas["room_send"]["properties"]
        for key in ("team_id", "room_id", "sender_agent_id", "target_spec", "payload", "actor_token"):
            self.assertIn(key, send_props)

        # room_receipts: team_id, room_id, agent_id, entry_ids, actor_token
        receipts_props = schemas["room_receipts"]["properties"]
        for key in ("team_id", "room_id", "agent_id", "entry_ids", "actor_token"):
            self.assertIn(key, receipts_props)

    # ------------------------------------------------------------------
    #  1. Unicast: target_spec = A2
    # ------------------------------------------------------------------
    def test_unicast_send_to_single_agent(self) -> None:
        """room_send target_spec=A2 returns exactly one receipt for A2."""
        result = self.dispatcher.call_tool(
            "room_send",
            {
                "team_id": TEAM_ID,
                "room_id": self.room_id,
                "sender_agent_id": OWNER_ID,
                "target_spec": AGENT_A2,
                "payload": {"type": "test.unicast", "body": "hello a2"},
                "actor_token": self.token_a1,
            },
        )

        # Envelope shape.
        envelope = result["envelope"]
        self.assertEqual(envelope["protocol"], "finalisma.a2a")
        self.assertEqual(envelope["version"], "2.0")
        self.assertIn("targets", envelope)
        self.assertEqual(envelope["targets"], [AGENT_A2])

        # Per-target idempotency key present.
        self.assertIn("per_target_idempotency_keys", envelope)
        self.assertIn(AGENT_A2, envelope["per_target_idempotency_keys"])

        # Exactly one receipt for A2.
        receipts = result["receipts"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["agent_id"], AGENT_A2)
        self.assertIn(receipts[0]["status"], ("queued", "in_flight"))
        self.assertIn("entry_id", receipts[0])

        # seq present.
        self.assertIn("seq", result)

    # ------------------------------------------------------------------
    #  2. Group: create group "reviewers", send to it
    # ------------------------------------------------------------------
    def test_group_send_expands_to_group_members_only(self) -> None:
        """room_send to group 'reviewers' returns receipts for A2 and A3, not A1."""
        # Build the group via room_groups action=add.
        self.dispatcher.call_tool(
            "room_groups",
            {
                "team_id": TEAM_ID,
                "room_id": self.room_id,
                "agent_id": OWNER_ID,
                "group_name": "reviewers",
                "action": "add",
                "members": [AGENT_A2, AGENT_A3],
                "actor_token": self.token_a1,
            },
        )

        result = self.dispatcher.call_tool(
            "room_send",
            {
                "team_id": TEAM_ID,
                "room_id": self.room_id,
                "sender_agent_id": OWNER_ID,
                "target_spec": "reviewers",
                "payload": {"type": "test.group", "body": "hey reviewers"},
                "actor_token": self.token_a1,
            },
        )

        receipts = result["receipts"]
        receipt_agents = {r["agent_id"] for r in receipts}

        # A2 and A3 receive; A1 (sender, not in group) does not.
        self.assertIn(AGENT_A2, receipt_agents)
        self.assertIn(AGENT_A3, receipt_agents)
        self.assertNotIn(OWNER_ID, receipt_agents)
        self.assertEqual(len(receipts), 2)

        for r in receipts:
            self.assertIn(r["status"], ("queued", "in_flight"))

    # ------------------------------------------------------------------
    #  3. broadcast "*" with exclude_sender default true
    # ------------------------------------------------------------------
    def test_broadcast_excludes_sender_by_default(self) -> None:
        """room_send target_spec='*' returns receipts for A2, A3 — not A1."""
        result = self.dispatcher.call_tool(
            "room_send",
            {
                "team_id": TEAM_ID,
                "room_id": self.room_id,
                "sender_agent_id": OWNER_ID,
                "target_spec": "*",
                "payload": {"type": "test.broadcast", "body": "hello all"},
                "actor_token": self.token_a1,
            },
        )

        receipts = result["receipts"]
        receipt_agents = {r["agent_id"] for r in receipts}

        # All OTHER active members receive; sender excluded by default.
        self.assertIn(AGENT_A2, receipt_agents)
        self.assertIn(AGENT_A3, receipt_agents)
        self.assertNotIn(OWNER_ID, receipt_agents)
        self.assertEqual(len(receipts), 2)

    # ------------------------------------------------------------------
    #  4. List of targets — deduplicated
    # ------------------------------------------------------------------
    def test_list_of_targets_is_deduplicated(self) -> None:
        """room_send target_spec=['A2','A3'] returns two receipts, no dupes."""
        result = self.dispatcher.call_tool(
            "room_send",
            {
                "team_id": TEAM_ID,
                "room_id": self.room_id,
                "sender_agent_id": OWNER_ID,
                "target_spec": [AGENT_A2, AGENT_A3],
                "payload": {"type": "test.list", "body": "hi both"},
                "actor_token": self.token_a1,
            },
        )

        receipts = result["receipts"]
        receipt_agents = [r["agent_id"] for r in receipts]

        # Exactly two receipts, one per target — no duplicates.
        self.assertEqual(len(receipts), 2)
        self.assertEqual(sorted(receipt_agents), sorted([AGENT_A2, AGENT_A3]))
        self.assertEqual(len(set(receipt_agents)), 2, "duplicate agent in receipts")

    # ------------------------------------------------------------------
    #  5. Receipts query after a send
    # ------------------------------------------------------------------
    def test_receipts_query_returns_status_per_entry(self) -> None:
        """room_receipts with returned entry_ids returns status per entry."""
        send_result = self.dispatcher.call_tool(
            "room_send",
            {
                "team_id": TEAM_ID,
                "room_id": self.room_id,
                "sender_agent_id": OWNER_ID,
                "target_spec": [AGENT_A2, AGENT_A3],
                "payload": {"type": "test.receipts", "body": "track me"},
                "actor_token": self.token_a1,
            },
        )

        entry_ids = [r["entry_id"] for r in send_result["receipts"]]
        self.assertEqual(len(entry_ids), 2)

        receipts_result = self.dispatcher.call_tool(
            "room_receipts",
            {
                "team_id": TEAM_ID,
                "room_id": self.room_id,
                "agent_id": OWNER_ID,
                "entry_ids": entry_ids,
                "actor_token": self.token_a1,
            },
        )

        queried = receipts_result["receipts"]
        self.assertEqual(len(queried), 2)
        queried_entry_ids = {r["entry_id"] for r in queried}
        self.assertEqual(queried_entry_ids, set(entry_ids))

        for r in queried:
            self.assertIn("entry_id", r)
            self.assertIn("status", r)
            self.assertIn("attempts", r)
            self.assertIn("next_attempt_at", r)
            self.assertIn("last_error", r)
            self.assertIn(r["status"], ("queued", "in_flight", "delivered", "dead"))

    # ------------------------------------------------------------------
    #  6. Schema presence (already covered above, keep explicit count)
    # ------------------------------------------------------------------
    def test_all_five_room_addressing_schemas_declared(self) -> None:
        """All 5 room-addressing tools appear in the TOOLS registry."""
        names = {tool["name"] for tool in TOOLS}
        for required in (
            "room_create",
            "room_join",
            "room_groups",
            "room_send",
            "room_receipts",
        ):
            self.assertIn(required, names, f"{required} not in TOOLS")


class RoomMessageKindTests(unittest.TestCase):
    """Sender-settable message_kind + message_kinds filter (MCP surface)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)

        reg_a1 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": TEAM_ID, "agent_id": OWNER_ID, "role": "owner"},
        )
        reg_a2 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": TEAM_ID, "agent_id": AGENT_A2, "role": "reviewer"},
        )
        reg_a3 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": TEAM_ID, "agent_id": AGENT_A3, "role": "reviewer"},
        )
        self.token_a1 = reg_a1["actor_token"]
        self.token_a2 = reg_a2["actor_token"]
        self.token_a3 = reg_a3["actor_token"]

        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": TEAM_ID, "owner_agent_id": OWNER_ID, "cap": ROOM_CAP},
        )
        self.room_id = created["room_id"]
        self.link_token = created["link_token"]
        for agent_id, token in ((OWNER_ID, self.token_a1), (AGENT_A2, self.token_a2),
                                (AGENT_A3, self.token_a3)):
            self.dispatcher.call_tool(
                "room_join",
                {
                    "team_id": TEAM_ID,
                    "room_id": self.room_id,
                    "link_token": self.link_token,
                    "agent_id": agent_id,
                    "consent": True,
                    "actor_token": token,
                },
            )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _send(self, sender: str, token: str, *, kind: str | None, text: str,
              target: Any = "*") -> dict[str, Any]:
        args = {
            "team_id": TEAM_ID,
            "room_id": self.room_id,
            "sender_agent_id": sender,
            "target_spec": target,
            "payload": {"text": text},
            "actor_token": token,
        }
        if kind is not None:
            args["message_kind"] = kind
        return self.dispatcher.call_tool("room_send", args)

    def _poll(self, agent: str, token: str, after_seq: int | None = 0,
              kinds: list[str] | None = None) -> dict[str, Any]:
        args = {
            "team_id": TEAM_ID,
            "room_id": self.room_id,
            "agent_id": agent,
            "actor_token": token,
        }
        if after_seq is not None:
            args["after_seq"] = after_seq
        if kinds is not None:
            args["message_kinds"] = kinds
        return self.dispatcher.call_tool("room_poll", args)

    @staticmethod
    def _messages(result: dict[str, Any]) -> list[dict[str, Any]]:
        return [e for e in result["events"] if e["kind"] == "room.message"]

    # ------------------------------------------------------------------
    # 1. tool schemas declare the new fields
    # ------------------------------------------------------------------
    def test_room_send_and_poll_schemas_declare_message_kind_fields(self) -> None:
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        send_props = schemas["room_send"]["properties"]
        self.assertIn("message_kind", send_props)
        self.assertNotIn("message_kind", schemas["room_send"]["required"])
        poll_props = schemas["room_poll"]["properties"]
        self.assertIn("message_kinds", poll_props)
        self.assertNotIn("message_kinds", schemas["room_poll"]["required"])

    # ------------------------------------------------------------------
    # 2. no message_kind -> unchanged behaviour, visible unfiltered
    # ------------------------------------------------------------------
    def test_send_without_message_kind_unchanged_and_visible_unfiltered(self) -> None:
        self._send(OWNER_ID, self.token_a1, kind=None, text="plain")
        result = self._poll(AGENT_A2, self.token_a2)
        plain = [e for e in self._messages(result)
                 if e["payload"]["payload"].get("text") == "plain"]
        self.assertEqual(len(plain), 1, "unfiltered poll must still see unlabelled sends")
        self.assertIsNone(plain[0]["message_kind"])

    # ------------------------------------------------------------------
    # 3. message_kind visible when matching, absent when not
    # ------------------------------------------------------------------
    def test_result_visible_when_filtering_on_result_absent_on_status(self) -> None:
        self._send(AGENT_A2, self.token_a2, kind="result", text="r1")
        on_result = self._poll(AGENT_A3, self.token_a3, kinds=["result"])
        r_events = self._messages(on_result)
        self.assertEqual(len(r_events), 1)
        self.assertEqual(r_events[0]["message_kind"], "result")
        self.assertEqual(r_events[0]["payload"]["payload"]["text"], "r1")
        on_status = self._poll(AGENT_A3, self.token_a3, kinds=["status"])
        self.assertEqual(len(self._messages(on_status)), 0)

    # ------------------------------------------------------------------
    # 4. invalid message_kind rejected with a clear error naming the argument
    # ------------------------------------------------------------------
    def test_invalid_message_kind_rejected_with_clear_error(self) -> None:
        for bad in ("UPPER", "x" * 33, "has spaces", "has.dot", ""):
            with self.assertRaises(WeftError) as ctx:
                self._send(OWNER_ID, self.token_a1, kind=bad, text="bad")
            self.assertEqual(ctx.exception.code, "invalid_argument")
            self.assertIn("message_kind", ctx.exception.message)

    # ------------------------------------------------------------------
    # 5. message_kinds filter validation
    # ------------------------------------------------------------------
    def test_message_kinds_filter_rejects_non_list_and_bad_entries(self) -> None:
        for bad in ("result", ["result", "BAD"], [""]):
            with self.assertRaises(WeftError) as ctx:
                self._poll(AGENT_A2, self.token_a2, kinds=bad)
            self.assertEqual(ctx.exception.code, "invalid_argument")
            self.assertIn("message_kinds", ctx.exception.message)

    # ------------------------------------------------------------------
    # 6. filtering and unfiltered callers agree on ordering; both advance
    #    without gaps or repeats
    # ------------------------------------------------------------------
    def test_filtering_and_unfiltered_callers_agree_and_advance(self) -> None:
        self._send(AGENT_A2, self.token_a2, kind="status", text="s1")
        self._send(AGENT_A3, self.token_a3, kind="result", text="r1")
        self._send(AGENT_A2, self.token_a2, kind="status", text="s2")
        self._send(AGENT_A3, self.token_a3, kind="result", text="r2")

        filtered = self._poll(AGENT_A3, self.token_a3, after_seq=0, kinds=["result"])
        unfiltered = self._poll(AGENT_A2, self.token_a2, after_seq=0)

        f_seqs = [e["seq"] for e in self._messages(filtered)]
        u_msgs = self._messages(unfiltered)
        u_seqs = [e["seq"] for e in u_msgs]
        u_result_seqs = [e["seq"] for e in u_msgs if e["message_kind"] == "result"]

        self.assertEqual(f_seqs, u_result_seqs)
        self.assertEqual(u_seqs, sorted(u_seqs))
        self.assertEqual(filtered["cursor_head"], unfiltered["cursor_head"])
        self.assertEqual(filtered["cursor_head"], max(u_seqs))
        self.assertEqual(filtered["next_seq"], f_seqs[-1] + 1)

        # Advance both cursors via ack (the durable-cursor mechanism).
        for agent, token, seq in ((AGENT_A3, self.token_a3, f_seqs[-1]),
                                  (AGENT_A2, self.token_a2, u_seqs[-1])):
            self.dispatcher.call_tool(
                "room_ack",
                {
                    "team_id": TEAM_ID,
                    "room_id": self.room_id,
                    "agent_id": agent,
                    "seq": seq,
                    "actor_token": token,
                },
            )

        # New interleaved messages.
        self._send(AGENT_A2, self.token_a2, kind="status", text="s3")
        self._send(AGENT_A3, self.token_a3, kind="result", text="r3")

        # Filtering caller resumes from its cursor (after_seq=None).
        filtered_again = self._poll(AGENT_A3, self.token_a3, after_seq=None,
                                    kinds=["result"])
        f2_seqs = [e["seq"] for e in self._messages(filtered_again)]
        self.assertEqual(f2_seqs, [f_seqs[-1] + 2],
                         "filtered resume must return exactly the one new result")
        self.assertGreater(f2_seqs[0], f_seqs[-1])

        # Unfiltered caller resumes from its cursor.
        unfiltered_again = self._poll(AGENT_A2, self.token_a2, after_seq=None)
        u2_seqs = [e["seq"] for e in self._messages(unfiltered_again)]
        self.assertEqual(u2_seqs, sorted(u2_seqs))
        self.assertEqual(len(u2_seqs), 2, "unfiltered resume must see both new messages")
        self.assertGreater(u2_seqs[0], u_seqs[-1])


if __name__ == "__main__":
    unittest.main()
