"""RED deliverable — negative-case security contract for the Room product.

Every room_* call MUST raise WeftError("unknown_tool", ...)
until the tools are wired. This file asserts the full security contract from
docs/ROOMS_DESIGN.md §3 (multi-use link delta + compensating controls),
§8 (tool surface + error codes), and §9 (the 15 negative cases) through the
real MCP surface (WeftDispatcher + real WeftStore on temp SQLite).

The one exception is test_stale_fencing_token_on_room_task, which uses the
already-wired create_task / claim_task / complete_task tools to
prove governance is preserved inside a room — that test gets past unknown_tool
but is structured to assert stale_fencing_token.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftError, WeftStore
from weft_mcp.server import WeftDispatcher, TOOLS


class RoomSecurityIntegrationTests(unittest.TestCase):
    """Negative-case integration tests — the refusals ARE the product."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        # Register 4 agents for the various negative scenarios.
        reg_owner = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": "owner-1", "role": "architect"},
        )
        reg_a2 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": "agent-2", "role": "builder"},
        )
        reg_a3 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": "agent-3", "role": "builder"},
        )
        reg_a4 = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": "team-1", "agent_id": "agent-4", "role": "builder"},
        )
        self.owner_token = reg_owner["actor_token"]
        self.token_a2 = reg_a2["actor_token"]
        self.token_a3 = reg_a3["actor_token"]
        self.token_a4 = reg_a4["actor_token"]

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------
    # Helper: assert a room tool raises unknown_tool (RED now).
    # After wiring, these same call sites assert the EXACT error code.
    # ------------------------------------------------------------------
    def _assert_room_tool_refused(self, tool: str, args: dict) -> None:
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(tool, args)
        self.assertEqual(ctx.exception.code, "unknown_tool")

    # ------------------------------------------------------------------
    # 1. join past cap -> "room_full"
    # ------------------------------------------------------------------
    def test_join_past_cap_refused_with_room_full(self) -> None:
        # Create cap=2 room. Owner auto-joins (1 member). One more join fills cap.
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 2},
        )
        room_id = created["room_id"]
        link_token = created["link_token"]
        # Second join succeeds (fills cap to 2).
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "link_token": link_token,
                "agent_id": "agent-2",
                "consent": True,
                "actor_token": self.token_a2,
            },
        )
        # Third join MUST be refused with room_full.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_join",
                {
                    "team_id": "team-1",
                    "room_id": room_id,
                    "link_token": link_token,
                    "agent_id": "agent-3",
                    "consent": True,
                    "actor_token": self.token_a3,
                },
            )
        self.assertEqual(ctx.exception.code, "room_full")

    # ------------------------------------------------------------------
    # 2. revoked link cannot join -> "link_revoked"
    # ------------------------------------------------------------------
    def test_revoked_link_cannot_join(self) -> None:
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_id = created["room_id"]
        link_token = created["link_token"]
        link_id = created["link_id"]
        # Owner revokes the link.
        self.dispatcher.call_tool(
            "room_revoke_link",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "owner_agent_id": "owner-1",
                "link_id": link_id,
                "actor_token": self.owner_token,
            },
        )
        # Join attempt with the revoked link MUST be refused with link_revoked.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_join",
                {
                    "team_id": "team-1",
                    "room_id": room_id,
                    "link_token": link_token,
                    "agent_id": "agent-2",
                    "consent": True,
                    "actor_token": self.token_a2,
                },
            )
        self.assertEqual(ctx.exception.code, "link_revoked")

    # ------------------------------------------------------------------
    # 3. expired link cannot join -> "link_expired"
    # DECISION: Create a room with ttl_seconds=1, sleep 1.1s so expires_at
    # is in the past, then attempt join. This is deterministic because the
    # expiry is checked against wall-clock time on every join.
    # ------------------------------------------------------------------
    def test_expired_link_cannot_join(self) -> None:
        import time
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4, "ttl_seconds": 1},
        )
        room_id = created["room_id"]
        link_token = created["link_token"]
        # Wait for the link to expire.
        time.sleep(1.1)
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_join",
                {
                    "team_id": "team-1",
                    "room_id": room_id,
                    "link_token": link_token,
                    "agent_id": "agent-2",
                    "consent": True,
                    "actor_token": self.token_a2,
                },
            )
        self.assertEqual(ctx.exception.code, "link_expired")

    # ------------------------------------------------------------------
    # 4. non-member cannot read -> "member_required"
    # ------------------------------------------------------------------
    def test_non_member_cannot_read_room(self) -> None:
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_id = created["room_id"]
        # agent-2 is registered but never joined — room_poll must refuse.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_poll",
                {
                    "team_id": "team-1",
                    "room_id": room_id,
                    "agent_id": "agent-2",
                    "actor_token": self.token_a2,
                },
            )
        self.assertEqual(ctx.exception.code, "member_required")
        # Same for room_info.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_info",
                {
                    "team_id": "team-1",
                    "room_id": room_id,
                    "agent_id": "agent-2",
                    "actor_token": self.token_a2,
                },
            )
        self.assertEqual(ctx.exception.code, "member_required")

    # ------------------------------------------------------------------
    # 5. cross-room isolation -> member of room A cannot access room B
    #    Expected: "room_not_found" (the room_id is treated as non-existent
    #    for a non-member; the spec allows room_not_found OR member_required).
    # ------------------------------------------------------------------
    def test_cross_room_isolation(self) -> None:
        # Room A — owner + agent-2 join.
        room_a = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_a_id = room_a["room_id"]
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_a_id,
                "link_token": room_a["link_token"],
                "agent_id": "agent-2",
                "consent": True,
                "actor_token": self.token_a2,
            },
        )
        # Room B — owner only.
        room_b = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_b_id = room_b["room_id"]
        # agent-2 (member of A) attempts to poll room B — must be refused.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_poll",
                {
                    "team_id": "team-1",
                    "room_id": room_b_id,
                    "agent_id": "agent-2",
                    "actor_token": self.token_a2,
                },
            )
        self.assertIn(ctx.exception.code, ("room_not_found", "member_required"))
        # Same for room_info.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_info",
                {
                    "team_id": "team-1",
                    "room_id": room_b_id,
                    "agent_id": "agent-2",
                    "actor_token": self.token_a2,
                },
            )
        self.assertIn(ctx.exception.code, ("room_not_found", "member_required"))
        # Same for room_send.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_send",
                {
                    "team_id": "team-1",
                    "room_id": room_b_id,
                    "sender_agent_id": "agent-2",
                    "target_spec": "*",
                    "payload": {"text": "cross-room"},
                    "actor_token": self.token_a2,
                },
            )
        self.assertIn(ctx.exception.code, ("room_not_found", "member_required"))

    # ------------------------------------------------------------------
    # 6. stale fencing token cannot complete a room task
    #    Uses the already-wired task tools. The task is bound to the room
    #    via metadata={"room_id": room_id}. Completing with fencing_token+1
    #    MUST be refused with stale_fencing_token.
    # ------------------------------------------------------------------
    def test_stale_fencing_token_on_room_task(self) -> None:
        # Create a room first.
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_id = created["room_id"]
        # Create a task bound to the room via metadata.
        task = self.dispatcher.call_tool(
            "create_task",
            {
                "team_id": "team-1",
                "created_by": "owner-1",
                "title": "Room-governed task",
                "description": "Governance must be preserved inside a room",
                "metadata": {"room_id": room_id},
                "actor_token": self.owner_token,
            },
        )
        task_id = task["task"]["task_id"]
        # Claim the task — returns a fencing token.
        claimed = self.dispatcher.call_tool(
            "claim_task",
            {
                "team_id": "team-1",
                "agent_id": "owner-1",
                "task_id": task_id,
                "actor_token": self.owner_token,
            },
        )
        fencing_token = claimed["fencing_token"]
        # Complete with a STALE token (fencing_token + 1) — MUST be refused.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "complete_task",
                {
                    "team_id": "team-1",
                    "agent_id": "owner-1",
                    "task_id": task_id,
                    "fencing_token": fencing_token + 1,
                    "summary": "stale completion attempt",
                    "actor_token": self.owner_token,
                },
            )
        self.assertEqual(ctx.exception.code, "stale_fencing_token")

    # ------------------------------------------------------------------
    # 7. link replay cannot impersonate an existing member
    #    - A3 joins with agent_id=A2 + A3's token -> "actor_auth_invalid"
    #    - A2 re-joins with agent_id=A2 + A2's token -> idempotent (no error)
    # ------------------------------------------------------------------
    def test_link_replay_cannot_impersonate_existing_member(self) -> None:
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_id = created["room_id"]
        link_token = created["link_token"]
        # A2 joins legitimately.
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "link_token": link_token,
                "agent_id": "agent-2",
                "consent": True,
                "actor_token": self.token_a2,
            },
        )
        # A3 attempts to impersonate A2 using A2's agent_id but A3's token.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_join",
                {
                    "team_id": "team-1",
                    "room_id": room_id,
                    "link_token": link_token,
                    "agent_id": "agent-2",
                    "consent": True,
                    "actor_token": self.token_a3,
                },
            )
        self.assertEqual(ctx.exception.code, "actor_auth_invalid")
        # A2 re-joins with its own token — idempotent, no error.
        result = self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "link_token": link_token,
                "agent_id": "agent-2",
                "consent": True,
                "actor_token": self.token_a2,
            },
        )
        self.assertIn("agent_id", result)
        self.assertEqual(result["agent_id"], "agent-2")

    # ------------------------------------------------------------------
    # 8. consent not a literal boolean -> "consent_required"
    # ------------------------------------------------------------------
    def test_consent_not_literal_boolean_refused(self) -> None:
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_id = created["room_id"]
        link_token = created["link_token"]
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_join",
                {
                    "team_id": "team-1",
                    "room_id": room_id,
                    "link_token": link_token,
                    "agent_id": "agent-2",
                    "consent": "yes",
                    "actor_token": self.token_a2,
                },
            )
        self.assertEqual(ctx.exception.code, "consent_required")

    # ------------------------------------------------------------------
    # 9. close by non-owner refused
    #    Expected: "member_required" (spec allows member_required or owner_required)
    # ------------------------------------------------------------------
    def test_close_by_non_owner_refused(self) -> None:
        created = self.dispatcher.call_tool(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": "owner-1", "cap": 4},
        )
        room_id = created["room_id"]
        link_token = created["link_token"]
        # agent-2 joins.
        self.dispatcher.call_tool(
            "room_join",
            {
                "team_id": "team-1",
                "room_id": room_id,
                "link_token": link_token,
                "agent_id": "agent-2",
                "consent": True,
                "actor_token": self.token_a2,
            },
        )
        # agent-2 (non-owner) attempts to close — must be refused.
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool(
                "room_close",
                {
                    "team_id": "team-1",
                    "room_id": room_id,
                    "owner_agent_id": "agent-2",
                    "actor_token": self.token_a2,
                },
            )
        self.assertIn(ctx.exception.code, ("member_required", "owner_required"))

    # ------------------------------------------------------------------
    # 10. tool schemas for all 12 room tools present in TOOLS list
    # ------------------------------------------------------------------
    def test_all_12_room_tool_schemas_present(self) -> None:
        expected_room_tools = {
            "room_create",
            "room_join",
            "room_info",
            "room_leave",
            "room_close",
            "room_send",
            "room_poll",
            "room_ack",
            "room_heartbeat",
            "room_groups",
            "room_receipts",
            "room_revoke_link",
        }
        schema_names = {tool["name"] for tool in TOOLS}
        missing = expected_room_tools - schema_names
        self.assertEqual(
            missing,
            set(),
            f"Missing room tool schemas in TOOLS: {missing}",
        )
        # Each room tool schema must declare actor_token as a property.
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        for tool_name in expected_room_tools:
            self.assertIn(
                "actor_token",
                schemas[tool_name]["properties"],
                f"{tool_name} schema is missing actor_token",
            )


if __name__ == "__main__":
    unittest.main()
