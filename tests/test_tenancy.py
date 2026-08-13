"""Tenancy / identity boundary layer tests (TDD).

Stdlib only. Uses temp-file SQLite so we never touch the real repo DB.
We open the same DB through a plain sqlite3 connection to verify
coexistence with core tables — we do NOT import core (avoids import cycles
and keeps this module's boundary clean).
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp import tenancy


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class InitIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_init_creates_tables(self):
        tenancy.init(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tenancy_%'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(
            tables,
            {"tenancy_orgs", "tenancy_org_members", "tenancy_claims"},
        )

    def test_init_is_idempotent(self):
        tenancy.init(self.db_path)
        tenancy.init(self.db_path)  # second call must not raise
        tenancy.init(self.db_path)  # third call still fine
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tenancy_%'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(
            tables,
            {"tenancy_orgs", "tenancy_org_members", "tenancy_claims"},
        )


class OrgCrudTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name
        tenancy.init(self.db_path)

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_create_and_list_orgs(self):
        org_id = tenancy.create_org(self.db_path, "Acme")
        self.assertTrue(org_id.startswith("org_"))
        orgs = tenancy.list_orgs(self.db_path)
        self.assertEqual(len(orgs), 1)
        self.assertEqual(orgs[0]["org_id"], org_id)
        self.assertEqual(orgs[0]["name"], "Acme")

    def test_create_org_requires_name(self):
        with self.assertRaises(ValueError):
            tenancy.create_org(self.db_path, "")

    def test_list_orgs_empty(self):
        self.assertEqual(tenancy.list_orgs(self.db_path), [])


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name
        tenancy.init(self.db_path)
        self.org_id = tenancy.create_org(self.db_path, "Acme")

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_add_and_check_member(self):
        tenancy.add_member(self.db_path, self.org_id, "agent_1", "admin")
        self.assertTrue(tenancy.is_member(self.db_path, self.org_id, "agent_1"))
        self.assertFalse(tenancy.is_member(self.db_path, self.org_id, "agent_2"))

    def test_add_member_unknown_org(self):
        with self.assertRaises(ValueError):
            tenancy.add_member(self.db_path, "org_does_not_exist", "agent_1")

    def test_remove_member(self):
        tenancy.add_member(self.db_path, self.org_id, "agent_1")
        self.assertTrue(tenancy.is_member(self.db_path, self.org_id, "agent_1"))
        tenancy.remove_member(self.db_path, self.org_id, "agent_1")
        self.assertFalse(tenancy.is_member(self.db_path, self.org_id, "agent_1"))

    def test_remove_member_noop_when_not_member(self):
        # Must not raise when removing a non-member.
        tenancy.remove_member(self.db_path, self.org_id, "ghost")

    def test_add_member_idempotent(self):
        tenancy.add_member(self.db_path, self.org_id, "agent_1", "admin")
        tenancy.add_member(self.db_path, self.org_id, "agent_1", "viewer")
        # Still a single membership row; role updated to latest.
        members = tenancy.list_members(self.db_path, self.org_id)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["role"], "viewer")

    def test_list_members(self):
        tenancy.add_member(self.db_path, self.org_id, "agent_1", "admin")
        tenancy.add_member(self.db_path, self.org_id, "agent_2", "viewer")
        members = tenancy.list_members(self.db_path, self.org_id)
        self.assertEqual(len(members), 2)
        by_id = {m["agent_id"]: m for m in members}
        self.assertEqual(by_id["agent_1"]["role"], "admin")
        self.assertEqual(by_id["agent_2"]["role"], "viewer")


class ActorKeyDerivationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name
        tenancy.init(self.db_path)
        self.org_id = tenancy.create_org(self.db_path, "Acme")

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_actor_key_stable(self):
        # actor_key = SHA-256(org_id + agent_id) — deterministic.
        key_a = tenancy.derive_actor_key(self.org_id, "agent_1")
        key_b = tenancy.derive_actor_key(self.org_id, "agent_1")
        self.assertEqual(key_a, key_b)
        self.assertEqual(key_a, _sha256(self.org_id + "agent_1"))
        self.assertEqual(len(key_a), 64)

    def test_actor_key_differs_across_orgs(self):
        other_org = tenancy.create_org(self.db_path, "Globex")
        key1 = tenancy.derive_actor_key(self.org_id, "agent_1")
        key2 = tenancy.derive_actor_key(other_org, "agent_1")
        self.assertNotEqual(key1, key2)

    def test_actor_key_differs_across_agents(self):
        key1 = tenancy.derive_actor_key(self.org_id, "agent_1")
        key2 = tenancy.derive_actor_key(self.org_id, "agent_2")
        self.assertNotEqual(key1, key2)


class ScopeEnforcementTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name
        tenancy.init(self.db_path)
        self.org_id = tenancy.create_org(self.db_path, "Acme")
        tenancy.add_member(self.db_path, self.org_id, "agent_1", "admin")
        self.valid_key = tenancy.derive_actor_key(self.org_id, "agent_1")

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_assert_scope_passes_for_member_with_valid_key(self):
        # Should not raise.
        tenancy.assert_scope(self.db_path, self.org_id, "agent_1", self.valid_key)

    def test_assert_scope_fails_with_wrong_key(self):
        with self.assertRaises(tenancy.ScopeError):
            tenancy.assert_scope(
                self.db_path,
                self.org_id,
                "agent_1",
                _sha256("tampered"),
            )

    def test_assert_scope_fails_for_non_member(self):
        outsider_key = tenancy.derive_actor_key(self.org_id, "outsider")
        with self.assertRaises(tenancy.ScopeError):
            tenancy.assert_scope(
                self.db_path,
                self.org_id,
                "outsider",
                outsider_key,
            )

    def test_assert_scope_fails_for_unknown_org(self):
        bogus_key = tenancy.derive_actor_key("org_bogus", "agent_1")
        with self.assertRaises(tenancy.ScopeError):
            tenancy.assert_scope(self.db_path, "org_bogus", "agent_1", bogus_key)


class OrgIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name
        tenancy.init(self.db_path)
        self.org_a = tenancy.create_org(self.db_path, "OrgA")
        self.org_b = tenancy.create_org(self.db_path, "OrgB")
        tenancy.add_member(self.db_path, self.org_a, "alice", "admin")
        tenancy.add_member(self.db_path, self.org_b, "bob", "admin")

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_members_do_not_leak_across_orgs(self):
        self.assertTrue(tenancy.is_member(self.db_path, self.org_a, "alice"))
        self.assertFalse(tenancy.is_member(self.db_path, self.org_b, "alice"))
        self.assertTrue(tenancy.is_member(self.db_path, self.org_b, "bob"))
        self.assertFalse(tenancy.is_member(self.db_path, self.org_a, "bob"))

    def test_scope_isolated_per_org(self):
        alice_key_a = tenancy.derive_actor_key(self.org_a, "alice")
        # Alice's valid key for org_a must NOT authorize her in org_b even
        # if someone reuses the raw hex.
        with self.assertRaises(tenancy.ScopeError):
            tenancy.assert_scope(self.db_path, self.org_b, "alice", alice_key_a)

    def test_claims_table_scoped_per_org(self):
        tenancy.claim_resource(self.db_path, self.org_a, "task", "t1", "alice")
        tenancy.claim_resource(self.db_path, self.org_b, "task", "t9", "bob")
        claims_a = tenancy.list_claims(self.db_path, self.org_a)
        claims_b = tenancy.list_claims(self.db_path, self.org_b)
        self.assertEqual({c["resource_id"] for c in claims_a}, {"t1"})
        self.assertEqual({c["resource_id"] for c in claims_b}, {"t9"})


class CoexistenceWithCoreTablesTests(unittest.TestCase):
    """The tenancy module must not disturb core tables — and vice versa."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_tenancy_tables_coexist_with_core_schema(self):
        # Simulate core creating its own tables first (schema_meta is the
        # canonical core table from core.py).
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS teams (team_id TEXT PRIMARY KEY, name TEXT NOT NULL)"
            )
        finally:
            conn.close()
        # Now init tenancy — must not clobber core tables.
        tenancy.init(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertIn("schema_meta", tables)
        self.assertIn("teams", tables)
        self.assertIn("tenancy_orgs", tables)

    def test_init_after_core_does_not_break_core_data(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3')")
            conn.commit()
        finally:
            conn.close()
        tenancy.init(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "3")


if __name__ == "__main__":
    unittest.main()
