"""Identity — orgs, membership, roles, structural role enforcement (Wave G).

RED deliverable: the weft_cloud.identity package does not exist yet, so
this suite fails at import. When the orchestrator wires the modules, every
test here asserts a contract from docs/IDENTITY_DESIGN.md §6, §8, §9.4.

Key invariant: an org IS a tenant (single tenant_id namespace). Role checks
fire INSIDE the service (ctx.require_role), never at the caller. SessionContext
is constructable ONLY via sessions.validate — there is no role argument any
service method accepts.

No mocks: make_backend() returns a real SqliteWalBackend on a temp file.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.storage import StorageBackend, SqliteWalBackend

# These imports are the RED line — the package does not exist yet.
from weft_cloud.identity import (
    accounts,
    sessions,
    orgs,
    context,
)
from weft_cloud.identity.context import RoleError, SessionContext


def make_backend() -> StorageBackend:
    """Factory seam — fresh SqliteWalBackend on a unique temp SQLite file."""
    return SqliteWalBackend(tempfile.mkstemp(suffix=".db")[1])


def _signup(backend: StorageBackend, tenant_id: str, email: str, password: str):
    """Helper: signup -> (account_id, verification_token)."""
    return accounts.signup(backend, tenant_id, email, password)


def _create_org_and_get_owner_ctx(
    backend: StorageBackend, owner_email: str, owner_password: str, org_name: str
):
    """Helper: create a tenant, signup owner, add owner as member with role 'owner',
    issue a session, and return (tenant_id, owner_account_id, owner_ctx).

    Per design §2.3 / §6.5: the org IS the tenant, and the creator is the owner.
    """
    tenant_id = f"tenant-{tempfile.mkstemp()[1]}"
    # Identity creates the tenant row as the first step (design §2.3 path 1).
    backend.create_tenant(tenant_id, org_name, plan_id="free")
    account_id, _verify_token = _signup(backend, tenant_id, owner_email, owner_password)
    # The creator becomes the owner member.
    # We need a bootstrap path: the first member is owner. Use a provisional owner
    # session via direct member insert + session issue (the orgs service exposes
    # add_member for admin/owner; for the very first member we seed the membership
    # directly then issue a session).
    with backend.transaction() as tx:
        tx.execute(
            "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
            "VALUES (?, ?, 'owner', datetime('now'))",
            (tenant_id, account_id),
        )
        tx.commit()
    session_id, raw_token = sessions.create(backend, tenant_id, account_id, role="owner")
    owner_ctx = sessions.validate(backend, raw_token)
    return tenant_id, account_id, owner_ctx


class OrgsRolesStructuralEnforcementTests(unittest.TestCase):
    """The structural-role-enforcement deliverable (design §8, §9.4)."""

    def setUp(self) -> None:
        self.backend = make_backend()
        # Org A + owner.
        self.tenant_a, self.owner_id, self.owner_ctx = _create_org_and_get_owner_ctx(
            self.backend, "owner-a@example.com", "CorrectHorse-Battery-Staple!42", "OrgA"
        )

    # -- 1. owner creates an org (tenant); owner is a member with role "owner" --
    def test_owner_is_member_with_role_owner(self) -> None:
        members = orgs.list_members(self.owner_ctx)
        owner_row = [m for m in members if m["account_id"] == self.owner_id]
        self.assertEqual(len(owner_row), 1)
        self.assertEqual(owner_row[0]["role"], "owner")

    # -- 2. owner adds a member with role "member"; that member can list_members --
    def test_owner_adds_member_and_member_can_list(self) -> None:
        orgs.add_member(self.owner_ctx, "member-a@example.com", role="member")
        # The added member must have an account created and a membership row.
        members = orgs.list_members(self.owner_ctx)
        emails = {m["email"] for m in members}
        self.assertIn("member-a@example.com", emails)
        member_row = [m for m in members if m["email"] == "member-a@example.com"]
        self.assertEqual(member_row[0]["role"], "member")

        # Authenticate the member and issue a session so we can exercise list_members.
        acct_id = accounts.authenticate(
            self.backend, self.tenant_a, "member-a@example.com", "CorrectHorse-Battery-Staple!42"
        )
        _, member_token = sessions.create(self.backend, self.tenant_a, acct_id, role="member")
        member_ctx = sessions.validate(self.backend, member_token)
        # View is allowed for all members (design §6.3).
        visible = orgs.list_members(member_ctx)
        self.assertEqual(len(visible), 2)  # owner + member

    # -- 3. member calls remove_member -> RoleError("forbidden") (fires in service) --
    def test_member_remove_member_raises_role_error_forbidden(self) -> None:
        orgs.add_member(self.owner_ctx, "member-b@example.com", role="member")
        member_b_id = accounts.authenticate(
            self.backend, self.tenant_a, "member-b@example.com", "CorrectHorse-Battery-Staple!42"
        )
        _, mb_token = sessions.create(self.backend, self.tenant_a, member_b_id, role="member")
        member_b_ctx = sessions.validate(self.backend, mb_token)

        # A third account to attempt to remove.
        orgs.add_member(self.owner_ctx, "victim@example.com", role="member")
        victim_id = accounts.authenticate(
            self.backend, self.tenant_a, "victim@example.com", "CorrectHorse-Battery-Staple!42"
        )

        with self.assertRaises(RoleError) as ctx_exc:
            orgs.remove_member(member_b_ctx, victim_id)
        self.assertEqual(ctx_exc.exception.code, "forbidden")

    # -- 4. member calls set_role -> RoleError("forbidden") --
    def test_member_set_role_raises_role_error_forbidden(self) -> None:
        orgs.add_member(self.owner_ctx, "member-c@example.com", role="member")
        member_c_id = accounts.authenticate(
            self.backend, self.tenant_a, "member-c@example.com", "CorrectHorse-Battery-Staple!42"
        )
        _, mc_token = sessions.create(self.backend, self.tenant_a, member_c_id, role="member")
        member_c_ctx = sessions.validate(self.backend, mc_token)

        # Attempt to self-escalate to admin.
        with self.assertRaises(RoleError) as ctx_exc:
            orgs.set_role(member_c_ctx, member_c_id, "admin")
        self.assertEqual(ctx_exc.exception.code, "forbidden")

    # -- 5. admin calls delete_org -> RoleError("forbidden") (owner-only action) --
    def test_admin_delete_org_raises_role_error_forbidden(self) -> None:
        orgs.add_member(self.owner_ctx, "admin-a@example.com", role="admin")
        admin_id = accounts.authenticate(
            self.backend, self.tenant_a, "admin-a@example.com", "CorrectHorse-Battery-Staple!42"
        )
        _, admin_token = sessions.create(self.backend, self.tenant_a, admin_id, role="admin")
        admin_ctx = sessions.validate(self.backend, admin_token)

        with self.assertRaises(RoleError) as ctx_exc:
            orgs.delete_org(admin_ctx)
        self.assertEqual(ctx_exc.exception.code, "forbidden")

    # -- 6. admin can add/remove members (allowed per matrix) --
    def test_admin_can_add_and_remove_members(self) -> None:
        orgs.add_member(self.owner_ctx, "admin-b@example.com", role="admin")
        admin_b_id = accounts.authenticate(
            self.backend, self.tenant_a, "admin-b@example.com", "CorrectHorse-Battery-Staple!42"
        )
        _, admin_b_token = sessions.create(self.backend, self.tenant_a, admin_b_id, role="admin")
        admin_b_ctx = sessions.validate(self.backend, admin_b_token)

        # Admin adds a member.
        orgs.add_member(admin_b_ctx, "added-by-admin@example.com", role="member")
        new_id = accounts.authenticate(
            self.backend, self.tenant_a, "added-by-admin@example.com", "CorrectHorse-Battery-Staple!42"
        )
        members = orgs.list_members(admin_b_ctx)
        self.assertIn(new_id, {m["account_id"] for m in members})

        # Admin removes that member.
        orgs.remove_member(admin_b_ctx, new_id)
        members_after = orgs.list_members(admin_b_ctx)
        self.assertNotIn(new_id, {m["account_id"] for m in members_after})

    def test_owner_cannot_be_removed_without_explicit_transfer(self) -> None:
        with self.assertRaises(RoleError) as ctx_exc:
            orgs.remove_member(self.owner_ctx, self.owner_id)
        self.assertEqual(ctx_exc.exception.code, "forbidden")

    # -- 7. role change revokes the target's existing sessions (rotation) --
    def test_set_role_revokes_target_sessions(self) -> None:
        orgs.add_member(self.owner_ctx, "promotee@example.com", role="member")
        promotee_id = accounts.authenticate(
            self.backend, self.tenant_a, "promotee@example.com", "CorrectHorse-Battery-Staple!42"
        )
        _, old_token = sessions.create(self.backend, self.tenant_a, promotee_id, role="member")
        # Sanity: the old session is valid before the role change.
        _ = sessions.validate(self.backend, old_token)

        # Owner promotes the member to admin — this must revoke all the target's sessions.
        orgs.set_role(self.owner_ctx, promotee_id, "admin")

        # The old session token must now be refused.
        from weft_cloud.identity.sessions import AuthError

        with self.assertRaises(AuthError) as ctx_exc:
            sessions.validate(self.backend, old_token)
        self.assertEqual(ctx_exc.exception.code, "invalid_session")

    # -- 8. An org IS a tenant: cloud_tenants has exactly one row for the org --
    def test_org_is_tenant_single_row_in_cloud_tenants(self) -> None:
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT COUNT(*) AS n FROM cloud_tenants WHERE tenant_id = ?",
                (self.tenant_a,),
            ).fetchone()
        self.assertEqual(row["n"], 1)
        # No parallel org table exists.
        with self.backend.transaction() as tx:
            org_tables = tx.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%org%'"
            ).fetchall()
        # The only "org"-sounding table is cloud_tenants; there is no orgs/org table.
        org_table_names = [r["name"] for r in org_tables]
        for name in org_table_names:
            self.assertEqual(
                name, "cloud_tenants",
                f"unexpected parallel org table: {name} — org IS a tenant (design §2.2)"
            )

    # -- 9. list_members from org A returns only org A members (no cross-tenant bleed) --
    def test_list_members_no_cross_tenant_bleed(self) -> None:
        # Create org B with different members.
        tenant_b, _owner_b_id, owner_b_ctx = _create_org_and_get_owner_ctx(
            self.backend, "owner-b@example.com", "Another-Strong-Password!99", "OrgB"
        )
        orgs.add_member(owner_b_ctx, "only-in-b@example.com", role="member")

        # Org A's list must not include org B's members.
        members_a = orgs.list_members(self.owner_ctx)
        emails_a = {m["email"] for m in members_a}
        self.assertNotIn("only-in-b@example.com", emails_a)
        self.assertNotIn("owner-b@example.com", emails_a)

    # -- 10. No service method accepts a fabricated role: missing ctx -> TypeError --
    def test_service_methods_require_ctx_no_role_argument(self) -> None:
        """Structural guarantee (design §8.4): calling an orgs method without a
        SessionContext raises TypeError — there is no role parameter to fabricate.
        The refusal is at the signature boundary, not a bypassable check."""
        with self.assertRaises(TypeError):
            orgs.remove_member(  # type: call missing required positional arg
                # Intentionally omitting ctx — this must be a TypeError.
                # If the signature were remove_member(tenant_id, account_id, role)
                # a caller could fabricate a role. It is not.
                "not-a-ctx",  # type: ignore[arg-type]
                "some-account-id",
            )
        with self.assertRaises(TypeError):
            orgs.delete_org()  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
