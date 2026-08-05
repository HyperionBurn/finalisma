from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finalisma_mcp.core import FinalismaError, FinalismaStore
from finalisma_mcp.server import FinalismaDispatcher, TOOLS


class TenancyIntegrationTests(unittest.TestCase):
    """Integration tests for the tenancy module driven through the REAL MCP
    JSON-RPC surface (FinalismaDispatcher.call_tool).

    The tenancy tool handlers are not wired yet, so every finalisma_org_*
    call_tool must raise FinalismaError("unknown_tool", ...). That RED is the
    deliverable.
    """

    TENANCY_TOOLS = (
        "finalisma_org_create",
        "finalisma_org_add_member",
        "finalisma_org_is_member",
        "finalisma_org_assert_scope",
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = FinalismaStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = FinalismaDispatcher(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    # -- schema-level assertions (mirror test_actor_credentials_server.py) --

    def test_tool_schemas_present_in_tools_registry(self) -> None:
        """All four tenancy tools must appear in TOOLS with an inputSchema."""
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        for tool_name in self.TENANCY_TOOLS:
            self.assertIn(tool_name, schemas, f"missing tool schema: {tool_name}")
            self.assertIsInstance(schemas[tool_name], dict)
            self.assertIn("properties", schemas[tool_name])

    # -- 1. create_org returns org_id; two orgs yield distinct ids --

    def test_create_org_returns_org_id_and_distinct_ids(self) -> None:
        first = self.dispatcher.call_tool(
            "finalisma_org_create",
            {"team_id": "team-1", "org_name": "Acme"},
        )
        self.assertIn("org_id", first)
        self.assertEqual(first["name"], "Acme")
        self.assertIn("created_at", first)

        second = self.dispatcher.call_tool(
            "finalisma_org_create",
            {"team_id": "team-1", "org_name": "Globex"},
        )
        self.assertIn("org_id", second)
        self.assertNotEqual(first["org_id"], second["org_id"])

    # -- 2. add_member then is_member -> True; non-member -> False --

    def test_add_member_then_is_member_true_and_false(self) -> None:
        created = self.dispatcher.call_tool(
            "finalisma_org_create",
            {"team_id": "team-1", "org_name": "Acme"},
        )
        org_id = created["org_id"]

        added = self.dispatcher.call_tool(
            "finalisma_org_add_member",
            {"team_id": "team-1", "org_id": org_id, "agent_id": "agent-a", "role": "owner"},
        )
        self.assertEqual(added["org_id"], org_id)
        self.assertEqual(added["agent_id"], "agent-a")
        self.assertEqual(added["role"], "owner")
        self.assertTrue(added["added"])

        member_check = self.dispatcher.call_tool(
            "finalisma_org_is_member",
            {"team_id": "team-1", "org_id": org_id, "agent_id": "agent-a"},
        )
        self.assertTrue(member_check["is_member"])

        non_member_check = self.dispatcher.call_tool(
            "finalisma_org_is_member",
            {"team_id": "team-1", "org_id": org_id, "agent_id": "agent-b"},
        )
        self.assertFalse(non_member_check["is_member"])

    # -- 3. assert_scope ok / raises for non-member / raises for wrong key --

    def test_assert_scope_ok_for_member_with_correct_key(self) -> None:
        created = self.dispatcher.call_tool(
            "finalisma_org_create",
            {"team_id": "team-1", "org_name": "Acme"},
        )
        org_id = created["org_id"]

        self.dispatcher.call_tool(
            "finalisma_org_add_member",
            {"team_id": "team-1", "org_id": org_id, "agent_id": "agent-a", "role": "owner"},
        )

        actor_key = hashlib.sha256((org_id + "agent-a").encode("utf-8")).hexdigest()
        result = self.dispatcher.call_tool(
            "finalisma_org_assert_scope",
            {"team_id": "team-1", "org_id": org_id, "agent_id": "agent-a", "actor_key_hex": actor_key},
        )
        self.assertTrue(result["ok"])

    def test_assert_scope_raises_for_non_member(self) -> None:
        created = self.dispatcher.call_tool(
            "finalisma_org_create",
            {"team_id": "team-1", "org_name": "Acme"},
        )
        org_id = created["org_id"]

        # Do NOT add agent-a as a member.
        actor_key = hashlib.sha256((org_id + "agent-a").encode("utf-8")).hexdigest()
        with self.assertRaises(FinalismaError) as ctx:
            self.dispatcher.call_tool(
                "finalisma_org_assert_scope",
                {"team_id": "team-1", "org_id": org_id, "agent_id": "agent-a", "actor_key_hex": actor_key},
            )
        self.assertEqual(ctx.exception.code, "tenancy_scope_forbidden")

    def test_assert_scope_raises_for_wrong_actor_key_cross_tenant(self) -> None:
        """Cross-tenant negative test: org B's agent cannot pass org A's scope."""
        org_a = self.dispatcher.call_tool(
            "finalisma_org_create",
            {"team_id": "team-1", "org_name": "OrgA"},
        )
        org_b = self.dispatcher.call_tool(
            "finalisma_org_create",
            {"team_id": "team-1", "org_name": "OrgB"},
        )
        org_a_id = org_a["org_id"]
        org_b_id = org_b["org_id"]

        self.dispatcher.call_tool(
            "finalisma_org_add_member",
            {"team_id": "team-1", "org_id": org_a_id, "agent_id": "agent-a", "role": "owner"},
        )
        self.dispatcher.call_tool(
            "finalisma_org_add_member",
            {"team_id": "team-1", "org_id": org_b_id, "agent_id": "agent-b", "role": "owner"},
        )

        # agent-b uses its OWN (org_b) key but tries to assert against org_a.
        wrong_key = hashlib.sha256((org_b_id + "agent-b").encode("utf-8")).hexdigest()
        with self.assertRaises(FinalismaError) as ctx:
            self.dispatcher.call_tool(
                "finalisma_org_assert_scope",
                {"team_id": "team-1", "org_id": org_a_id, "agent_id": "agent-b", "actor_key_hex": wrong_key},
            )
        self.assertEqual(ctx.exception.code, "tenancy_scope_forbidden")

    # -- 4. Cross-tenant isolation: two orgs, separate members --

    def test_cross_tenant_isolation_two_orgs(self) -> None:
        org_a = self.dispatcher.call_tool(
            "finalisma_org_create",
            {"team_id": "team-1", "org_name": "OrgA"},
        )
        org_b = self.dispatcher.call_tool(
            "finalisma_org_create",
            {"team_id": "team-1", "org_name": "OrgB"},
        )
        org_a_id = org_a["org_id"]
        org_b_id = org_b["org_id"]

        self.dispatcher.call_tool(
            "finalisma_org_add_member",
            {"team_id": "team-1", "org_id": org_a_id, "agent_id": "agent-a", "role": "owner"},
        )
        self.dispatcher.call_tool(
            "finalisma_org_add_member",
            {"team_id": "team-1", "org_id": org_b_id, "agent_id": "agent-b", "role": "owner"},
        )

        # Each member is a member only of its own org.
        a_in_a = self.dispatcher.call_tool(
            "finalisma_org_is_member",
            {"team_id": "team-1", "org_id": org_a_id, "agent_id": "agent-a"},
        )
        self.assertTrue(a_in_a["is_member"])
        a_in_b = self.dispatcher.call_tool(
            "finalisma_org_is_member",
            {"team_id": "team-1", "org_id": org_b_id, "agent_id": "agent-a"},
        )
        self.assertFalse(a_in_b["is_member"])
        b_in_b = self.dispatcher.call_tool(
            "finalisma_org_is_member",
            {"team_id": "team-1", "org_id": org_b_id, "agent_id": "agent-b"},
        )
        self.assertTrue(b_in_b["is_member"])
        b_in_a = self.dispatcher.call_tool(
            "finalisma_org_is_member",
            {"team_id": "team-1", "org_id": org_a_id, "agent_id": "agent-b"},
        )
        self.assertFalse(b_in_a["is_member"])

        # Each member's scope passes only in its own org.
        key_a = hashlib.sha256((org_a_id + "agent-a").encode("utf-8")).hexdigest()
        key_b = hashlib.sha256((org_b_id + "agent-b").encode("utf-8")).hexdigest()

        scope_a = self.dispatcher.call_tool(
            "finalisma_org_assert_scope",
            {"team_id": "team-1", "org_id": org_a_id, "agent_id": "agent-a", "actor_key_hex": key_a},
        )
        self.assertTrue(scope_a["ok"])
        scope_b = self.dispatcher.call_tool(
            "finalisma_org_assert_scope",
            {"team_id": "team-1", "org_id": org_b_id, "agent_id": "agent-b", "actor_key_hex": key_b},
        )
        self.assertTrue(scope_b["ok"])

        # Cross-org: agent-a's key fails against org_b and vice versa.
        with self.assertRaises(FinalismaError) as cross_a_in_b:
            self.dispatcher.call_tool(
                "finalisma_org_assert_scope",
                {"team_id": "team-1", "org_id": org_b_id, "agent_id": "agent-a", "actor_key_hex": key_a},
            )
        self.assertEqual(cross_a_in_b.exception.code, "tenancy_scope_forbidden")

        with self.assertRaises(FinalismaError) as cross_b_in_a:
            self.dispatcher.call_tool(
                "finalisma_org_assert_scope",
                {"team_id": "team-1", "org_id": org_a_id, "agent_id": "agent-b", "actor_key_hex": key_b},
            )
        self.assertEqual(cross_b_in_a.exception.code, "tenancy_scope_forbidden")


if __name__ == "__main__":
    unittest.main()
