from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.identity import accounts, agent_keys, orgs, sessions
from weft_cloud.identity.sessions import AuthError
from weft_cloud.storage import SqliteWalBackend
from weft_cloud.rooms import CloudRoomService


def _make_backend() -> SqliteWalBackend:
    return SqliteWalBackend(tempfile.mkstemp(suffix=".db")[1])


def _make_owner(backend: SqliteWalBackend, tenant_id: str, email: str):
    backend.create_tenant(tenant_id, email, plan_id="free")
    account_id, _ = accounts.signup(backend, tenant_id, email, "CorrectHorse-Battery-Staple!42")
    with backend.transaction() as tx:
        tx.execute(
            "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
            "VALUES (?, ?, 'owner', datetime('now'))",
            (tenant_id, account_id),
        )
        tx.commit()
    _, raw_session = sessions.create(backend, tenant_id, account_id, role="owner")
    return account_id, sessions.validate(backend, raw_session)


class IdentityOrgDeleteTests(unittest.TestCase):
    def test_delete_org_removes_credentials_rooms_and_tenant_state_atomically(self) -> None:
        backend = _make_backend()
        tenant_id, other_tenant_id = "tenant-delete", "tenant-keep"
        owner_id, owner_ctx = _make_owner(backend, tenant_id, "owner-delete@example.com")
        other_owner_id, _ = _make_owner(backend, other_tenant_id, "owner-keep@example.com")
        key_id, raw_key = agent_keys.create(backend, tenant_id, owner_id, label="delete-me")
        CloudRoomService(backend).create_room(
            tenant_id, key_id, raw_key, cap=2, name="delete-me"
        )
        other_key_id, other_raw_key = agent_keys.create(backend, other_tenant_id, other_owner_id)
        other_room = CloudRoomService(backend).create_room(
            other_tenant_id, other_key_id, other_raw_key, cap=2, name="keep-me"
        )

        orgs.delete_org(owner_ctx)

        tenant_tables = (
            "cloud_tenants",
            "cloud_identity_accounts",
            "cloud_identity_sessions",
            "cloud_identity_members",
            "cloud_identity_agent_keys",
            "cloud_identity_invites",
            "cloud_identity_outbox",
            "cloud_rooms",
            "cloud_room_members",
            "cloud_room_links",
            "cloud_room_event_log",
            "cloud_room_cursors",
            "cloud_room_groups",
            "cloud_room_group_members",
            "cloud_room_receipts",
            "cloud_tenant_rooms",
            "cloud_counters",
            "cloud_room_counters",
            "cloud_rate_windows",
            "cloud_outbox",
            "cloud_audit",
            "cloud_event_mirror",
        )
        with backend.transaction() as tx:
            for table in tenant_tables:
                row = tx.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()
                self.assertEqual(row["n"], 0, table)
            kept = tx.execute(
                "SELECT COUNT(*) AS n FROM cloud_rooms WHERE tenant_id = ? AND room_id = ?",
                (other_tenant_id, other_room["room_id"]),
            ).fetchone()
            self.assertEqual(kept["n"], 1)
            kept_key = tx.execute(
                "SELECT COUNT(*) AS n FROM cloud_identity_agent_keys WHERE tenant_id = ? AND key_id = ?",
                (other_tenant_id, other_key_id),
            ).fetchone()
            self.assertEqual(kept_key["n"], 1)

        with self.assertRaises(AuthError) as exc:
            agent_keys.validate(backend, raw_key)
        self.assertEqual(exc.exception.code, "invalid_session")


if __name__ == "__main__":
    unittest.main()
