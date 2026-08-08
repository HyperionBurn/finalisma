"""Tests for the Weft N-way roster layer.

Stdlib only. Temp SQLite files. Mirrors core's stale-agent semantics.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Ensure src is importable without installing the package.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from weft_mcp import roster  # noqa: E402


class RosterFreshDbTests(unittest.TestCase):
    """Each test gets its own temp SQLite file."""

    def setUp(self):
        handle, path = tempfile.mkstemp(suffix=".db", prefix="roster-test-")
        os.close(handle)
        self.db_path = path
        roster.init(self.db_path)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    def test_create_roster_returns_id_and_registers_owner(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        self.assertTrue(rid.startswith("roster_"))
        members = roster.list_members(rid)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["agent_id"], "agent-a")
        self.assertEqual(members[0]["status"], "active")

    def test_join_and_leave(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        roster.join_roster(rid, "agent-b", capabilities_json=["read", "comment"])
        ids = {m["agent_id"] for m in roster.list_members(rid)}
        self.assertEqual(ids, {"agent-a", "agent-b"})
        roster.leave_roster(rid, "agent-b")
        ids = {m["agent_id"] for m in roster.list_members(rid)}
        self.assertEqual(ids, {"agent-a"})

    def test_join_is_idempotent(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        roster.join_roster(rid, "agent-b", capabilities_json=["read"])
        roster.join_roster(rid, "agent-b", capabilities_json=["read", "write"])
        # Capabilities should reflect the latest join.
        caps = roster.get_capabilities(rid, "agent-b")
        self.assertEqual(caps, ["read", "write"])
        self.assertEqual(len(roster.list_members(rid)), 2)

    def test_member_status_heartbeat_and_staleness(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        roster.join_roster(rid, "agent-b", capabilities_json=[])
        self.assertEqual(roster.member_status(rid, "agent-b"), "active")
        # Force staleness by back-dating last_seen.
        roster._backdate_heartbeat(rid, "agent-b", seconds=3600)
        self.assertEqual(roster.member_status(rid, "agent-b"), "stale")

    def test_set_capabilities(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        roster.set_capabilities(rid, "agent-a", capabilities_json=["admin"])
        self.assertEqual(roster.get_capabilities(rid, "agent-a"), ["admin"])

    def test_group_add_remove_list(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        roster.join_roster(rid, "agent-b", capabilities_json=[])
        roster.join_roster(rid, "agent-c", capabilities_json=[])
        roster.add_to_group(rid, "reviewers", "agent-b")
        roster.add_to_group(rid, "reviewers", "agent-c")
        members = roster.list_group(rid, "reviewers")
        self.assertEqual(set(members), {"agent-b", "agent-c"})
        roster.remove_from_group(rid, "reviewers", "agent-b")
        self.assertEqual(roster.list_group(rid, "reviewers"), ["agent-c"])

    def test_route_targets_agent(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        roster.join_roster(rid, "agent-b", capabilities_json=[])
        roster.join_roster(rid, "agent-c", capabilities_json=[])
        targets = roster.route_targets(rid, "agent-b")
        self.assertEqual(targets, ["agent-b"])

    def test_route_targets_group(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        roster.join_roster(rid, "agent-b", capabilities_json=[])
        roster.join_roster(rid, "agent-c", capabilities_json=[])
        roster.add_to_group(rid, "reviewers", "agent-b")
        roster.add_to_group(rid, "reviewers", "agent-c")
        targets = sorted(roster.route_targets(rid, "reviewers"))
        self.assertEqual(targets, ["agent-b", "agent-c"])

    def test_route_targets_broadcast(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        roster.join_roster(rid, "agent-b", capabilities_json=[])
        roster.join_roster(rid, "agent-c", capabilities_json=[])
        targets = sorted(roster.route_targets(rid, "*"))
        self.assertEqual(targets, ["agent-a", "agent-b", "agent-c"])

    def test_route_targets_list(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        roster.join_roster(rid, "agent-b", capabilities_json=[])
        roster.join_roster(rid, "agent-c", capabilities_json=[])
        targets = sorted(roster.route_targets(rid, ["agent-a", "agent-c"]))
        self.assertEqual(targets, ["agent-a", "agent-c"])

    def test_route_targets_excludes_stale(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        roster.join_roster(rid, "agent-b", capabilities_json=[])
        roster.join_roster(rid, "agent-c", capabilities_json=[])
        roster._backdate_heartbeat(rid, "agent-b", seconds=3600)
        targets = sorted(roster.route_targets(rid, "*"))
        self.assertEqual(targets, ["agent-a", "agent-c"])

    def test_route_targets_unknown_group_returns_empty(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        self.assertEqual(roster.route_targets(rid, "ghost-group"), [])

    def test_link_one_use_semantics(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        link_id = roster.create_link(rid, created_by="agent-a", ttl_seconds=60)
        # First consume succeeds.
        self.assertTrue(roster.consume_link(rid, link_id))
        # Second consume fails (one-use).
        self.assertFalse(roster.consume_link(rid, link_id))

    def test_link_expiry(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        link_id = roster.create_link(rid, created_by="agent-a", ttl_seconds=1)
        time.sleep(1.1)
        self.assertFalse(roster.consume_link(rid, link_id))

    def test_link_unknown_id_fails(self):
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        self.assertFalse(roster.consume_link(rid, "does-not-exist"))

    def test_envelope_assembly_fields(self):
        env = roster.build_envelope_v2(
            sender="agent-a",
            targets=["agent-b", "agent-c"],
            type="task.progress",
            payload={"progress": 50},
            capabilities=["coding"],
        )
        self.assertEqual(env["protocol"], "weft.a2a")
        self.assertEqual(env["version"], "2.0")
        self.assertEqual(env["type"], "task.progress")
        self.assertEqual(env["sender"], {"agent_id": "agent-a"})
        self.assertEqual(env["targets"], ["agent-b", "agent-c"])
        self.assertIn("correlation_id", env)
        self.assertIn("trace_id", env)
        self.assertIn("timestamp", env)
        self.assertEqual(env["payload"], {"progress": 50})
        self.assertEqual(env["capabilities"], ["coding"])
        # Per-target idempotency keys must differ.
        keys = [t["idempotency_key"] for t in env["per_target"]]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])

    def test_envelope_per_target_keys_unique_across_calls(self):
        env1 = roster.build_envelope_v2("a", ["b"], "x", {}, [])
        env2 = roster.build_envelope_v2("a", ["b"], "x", {}, [])
        self.assertNotEqual(
            env1["per_target"][0]["idempotency_key"],
            env2["per_target"][0]["idempotency_key"],
        )

    def test_isolation_between_rosters(self):
        r1 = roster.create_roster(owner_agent_id="a1", team_id="demo")
        r2 = roster.create_roster(owner_agent_id="a2", team_id="demo")
        roster.join_roster(r1, "shared", capabilities_json=[])
        roster.join_roster(r2, "shared", capabilities_json=[])
        roster.leave_roster(r1, "shared")
        self.assertEqual(len(roster.list_members(r1)), 1)
        self.assertEqual(len(roster.list_members(r2)), 2)

    def test_init_is_idempotent(self):
        roster.init(self.db_path)
        roster.init(self.db_path)
        rid = roster.create_roster(owner_agent_id="agent-a", team_id="demo")
        self.assertTrue(rid.startswith("roster_"))


class RosterCoexistenceWithCoreTests(unittest.TestCase):
    """Opening the same SQLite file used by core must not clash."""

    def setUp(self):
        handle, path = tempfile.mkstemp(suffix=".db", prefix="roster-core-")
        os.close(handle)
        self.db_path = path

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    def test_core_then_roster_same_db(self):
        from weft_mcp.core import WeftStore

        store = WeftStore(state_path=self.db_path, workspace_path=self.db_path + ".ws")
        try:
            store.register_agent(team_id="demo", agent_id="core-agent", role="tester")
            # Now init the roster layer on the same file.
            roster.init(self.db_path)
            rid = roster.create_roster(owner_agent_id="core-agent", team_id="demo")
            roster.join_roster(rid, "roster-agent", capabilities_json=["read"])
            member_ids = {m["agent_id"] for m in roster.list_members(rid)}
            self.assertEqual(member_ids, {"core-agent", "roster-agent"})
            # Core's agents table is untouched.
            status = store.team_status("demo")
            self.assertEqual(len(status["agents"]), 1)
            self.assertEqual(status["agents"][0]["agent_id"], "core-agent")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
