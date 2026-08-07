"""Integration tests for the Finalisma N-way roster MCP surface.

Drives roster through the REAL MCP JSON-RPC dispatcher via
``FinalismaDispatcher.call_tool`` — never ``roster.py``'s Python API directly.

These tests are RED by design: the ``finalisma_roster_*`` tools are not yet
registered on the dispatcher. The orchestrator wires them; these tests lock
the contract (tool names, argument names, and return shapes).

No mocks for the SQLite layer — we test against a real on-disk store, exactly
like ``tests/test_actor_credentials_server.py``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import FinalismaStore
from weft_mcp.server import FinalismaDispatcher, TOOLS


# The five roster tool names the orchestrator must register.
ROSTER_TOOLS = (
    "finalisma_roster_create",
    "finalisma_roster_join",
    "finalisma_roster_members",
    "finalisma_roster_route",
    "finalisma_roster_group_add",
)


class RosterIntegrationTests(unittest.TestCase):
    """End-to-end roster contract exercised through the MCP dispatcher."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        # Mirror the reference harness exactly.
        self.store = FinalismaStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = FinalismaDispatcher(self.store)
        self.team_id = "demo"
        # Owner + two joining agents.
        self.owner = "owner-agent"
        self.agent_a = "agent-a"
        self.agent_b = "agent-b"

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _create_roster(self, owner_agent_id: str | None = None) -> dict:
        return self.dispatcher.call_tool(
            "finalisma_roster_create",
            {"team_id": self.team_id, "owner_agent_id": owner_agent_id or self.owner},
        )

    def _join(self, roster_id: str, agent_id: str, capabilities: list[str] | None = None) -> dict:
        return self.dispatcher.call_tool(
            "finalisma_roster_join",
            {
                "team_id": self.team_id,
                "roster_id": roster_id,
                "agent_id": agent_id,
                "capabilities": capabilities or [],
            },
        )

    # ------------------------------------------------------------------
    # 1. create_roster returns roster_id
    # ------------------------------------------------------------------
    def test_create_roster_returns_roster_id(self) -> None:
        result = self._create_roster()
        self.assertIn("roster_id", result)
        self.assertIn("owner_agent_id", result)
        self.assertIn("created_at", result)
        self.assertEqual(result["owner_agent_id"], self.owner)
        self.assertTrue(str(result["roster_id"]).startswith("roster_"))

    # ------------------------------------------------------------------
    # 2. join_roster twice, members lists both with status "active"
    # ------------------------------------------------------------------
    def test_join_twice_members_lists_both_active(self) -> None:
        created = self._create_roster()
        roster_id = created["roster_id"]

        join_a = self._join(roster_id, self.agent_a, capabilities=["build"])
        self.assertEqual(join_a["agent_id"], self.agent_a)
        self.assertEqual(join_a["status"], "active")
        self.assertIn("joined_at", join_a)

        join_b = self._join(roster_id, self.agent_b, capabilities=["review"])
        self.assertEqual(join_b["agent_id"], self.agent_b)
        self.assertEqual(join_b["status"], "active")

        members = self.dispatcher.call_tool(
            "finalisma_roster_members",
            {"team_id": self.team_id, "roster_id": roster_id},
        )
        self.assertEqual(members["roster_id"], roster_id)
        agent_ids = {m["agent_id"] for m in members["members"]}
        self.assertIn(self.owner, agent_ids)
        self.assertIn(self.agent_a, agent_ids)
        self.assertIn(self.agent_b, agent_ids)
        # Every listed member reports active status.
        for m in members["members"]:
            self.assertEqual(m["status"], "active", m["agent_id"])
        # Capabilities round-trip.
        caps_by_id = {m["agent_id"]: m["capabilities"] for m in members["members"]}
        self.assertEqual(caps_by_id[self.agent_a], ["build"])
        self.assertEqual(caps_by_id[self.agent_b], ["review"])

    # ------------------------------------------------------------------
    # 3. route_targets: "*" -> all active; single agent -> that agent;
    #    unknown agent -> empty
    # ------------------------------------------------------------------
    def test_route_targets_broadcast_single_and_unknown(self) -> None:
        created = self._create_roster()
        roster_id = created["roster_id"]
        self._join(roster_id, self.agent_a)
        self._join(roster_id, self.agent_b)

        # Broadcast returns every active member (owner + a + b).
        broadcast = self.dispatcher.call_tool(
            "finalisma_roster_route",
            {"team_id": self.team_id, "roster_id": roster_id, "target_spec": "*"},
        )
        self.assertEqual(broadcast["roster_id"], roster_id)
        self.assertEqual(set(broadcast["targets"]), {self.owner, self.agent_a, self.agent_b})

        # Single known agent routes to just that agent.
        single = self.dispatcher.call_tool(
            "finalisma_roster_route",
            {"team_id": self.team_id, "roster_id": roster_id, "target_spec": self.agent_a},
        )
        self.assertEqual(single["targets"], [self.agent_a])

        # Unknown agent yields an empty target list.
        unknown = self.dispatcher.call_tool(
            "finalisma_roster_route",
            {"team_id": self.team_id, "roster_id": roster_id, "target_spec": "ghost-agent"},
        )
        self.assertEqual(unknown["targets"], [])

    # ------------------------------------------------------------------
    # 4. group_add then route_targets with the group name returns that agent
    # ------------------------------------------------------------------
    def test_group_add_then_route_by_group(self) -> None:
        created = self._create_roster()
        roster_id = created["roster_id"]
        self._join(roster_id, self.agent_a)
        self._join(roster_id, self.agent_b)

        add = self.dispatcher.call_tool(
            "finalisma_roster_group_add",
            {
                "team_id": self.team_id,
                "roster_id": roster_id,
                "group_name": "builders",
                "agent_id": self.agent_a,
            },
        )
        self.assertEqual(add["group_name"], "builders")
        self.assertEqual(add["agent_id"], self.agent_a)
        self.assertTrue(add["added"])

        routed = self.dispatcher.call_tool(
            "finalisma_roster_route",
            {"team_id": self.team_id, "roster_id": roster_id, "target_spec": "builders"},
        )
        self.assertEqual(routed["targets"], [self.agent_a])

    # ------------------------------------------------------------------
    # 5. Tool schemas present in TOOLS for all five roster tool names
    # ------------------------------------------------------------------
    def test_roster_tool_schemas_present(self) -> None:
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        for tool_name in ROSTER_TOOLS:
            self.assertIn(tool_name, schemas, f"missing schema for {tool_name}")
            schema = schemas[tool_name]
            self.assertIn("properties", schema)
        # Spot-check the create contract's argument names.
        create_props = schemas["finalisma_roster_create"]["properties"]
        self.assertIn("team_id", create_props)
        self.assertIn("owner_agent_id", create_props)

    # ------------------------------------------------------------------
    # 6. roster_route "*" returns joined (active) members.
    #    Full stale-exclusion is covered at the roster unit layer; here we
    #    assert the broadcast surface returns everyone who joined active.
    # ------------------------------------------------------------------
    def test_route_broadcast_returns_all_active_members(self) -> None:
        created = self._create_roster()
        roster_id = created["roster_id"]
        self._join(roster_id, self.agent_a)
        self._join(roster_id, self.agent_b)

        routed = self.dispatcher.call_tool(
            "finalisma_roster_route",
            {"team_id": self.team_id, "roster_id": roster_id, "target_spec": "*"},
        )
        self.assertEqual(
            set(routed["targets"]),
            {self.owner, self.agent_a, self.agent_b},
            "broadcast '*' must return every active member",
        )


if __name__ == "__main__":
    unittest.main()
