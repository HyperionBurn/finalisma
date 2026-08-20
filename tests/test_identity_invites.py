"""Wave G — Identity: invites contract (RED).

Drives the invites module through the real identity API against a real
SqliteWalBackend on a temp file. The ``weft_cloud.identity.invites``
module does not exist yet, so the import fails — that ModuleNotFoundError is
the RED deliverable.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 7, 9.5, 13.

Contract under test:
  - create(ctx, email, role) -> (invite_id, raw_fiv_token); raw token hashed
    at rest (token_hash = sha256(raw)), never stored raw.
  - create requires admin/owner via the role gate; a member is refused.
  - the invite's role is the ONLY role ever granted on accept (role-scoped,
    no self-escalation); the accepter never supplies a role.
  - accept(backend, raw_token, email, password) -> (account_id, session).
  - wrong email   -> AuthError("invite_mismatch")
  - accept twice  -> AuthError("invite_consumed")
  - expired       -> AuthError("invite_expired")
  - unknown/garbage token -> AuthError("invite_expired") (uniform, no oracle)
  - defence in depth: a SessionContext constructed with a fabricated role
    (owner) whose DB membership is only 'member' is still refused — the role
    gate re-derives the actor's role from cloud_identity_members, not from the
    context field alone.

No mocks: make_backend() returns a real SqliteWalBackend on a temp file.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# These imports are the RED line — the identity package does not exist yet.
from weft_cloud.identity import invites, orgs, sessions  # noqa: E402
from weft_cloud.identity.context import RoleError, SessionContext  # noqa: E402
from weft_cloud.identity.tokens import AuthError  # noqa: E402
from weft_cloud.migrations import apply_migrations  # noqa: E402
from weft_cloud.storage import SqliteWalBackend  # noqa: E402


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex}"


def make_backend() -> SqliteWalBackend:
    """Test seam: real SqliteWalBackend on a temp file, migrations applied."""
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "identity.db"
    backend = SqliteWalBackend(db_path)
    backend.initialize()
    apply_migrations(backend)
    backend._tmpdir = tmp  # type: ignore[attr-defined]
    return backend


def _seed_owner(backend: SqliteWalBackend, tenant_id: str, email: str, password: str):
    """signup + owner membership + owner session context. Returns ctx."""
    from weft_cloud.identity import accounts

    account_id, _ = accounts.signup(backend, tenant_id, email, password)
    with backend.transaction() as tx:
        tx.execute(
            "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
            "VALUES (?, ?, 'owner', datetime('now'))",
            (tenant_id, account_id),
        )
        tx.commit()
    _, raw_token = sessions.create(backend, tenant_id, account_id, role="owner")
    return sessions.validate(backend, raw_token)


class IdentityInvitesContractTests(unittest.TestCase):
    """Invites contract through the real identity API (design section 7)."""

    PASSWORD = "CorrectHorse-Battery-Staple!42"

    def setUp(self) -> None:
        self.backend = make_backend()
        self.tenant_id = _new_id("tenant")
        self.owner_ctx = _seed_owner(
            self.backend, self.tenant_id, "owner@example.com", self.PASSWORD
        )

    def tearDown(self) -> None:
        try:
            self.backend.close()
        finally:
            tmp = getattr(self.backend, "_tmpdir", None)
            if tmp is not None:
                tmp.cleanup()

    # -- 1. create returns fiv_ token + invite_id; raw token NOT stored --

    def test_create_returns_token_and_invite_id_raw_not_stored(self) -> None:
        invite_id, raw_token = invites.create(
            self.owner_ctx, "guest@example.com", role="member"
        )
        self.assertTrue(invite_id.startswith("inv_"))
        self.assertTrue(raw_token.startswith("fiv_"))

        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT token_hash, tenant_id, email, role, created_by "
                "FROM cloud_identity_invites WHERE invite_id = ?",
                (invite_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["token_hash"], _sha256(raw_token))
        self.assertEqual(row["tenant_id"], self.tenant_id)
        self.assertEqual(row["email"], "guest@example.com")
        self.assertEqual(row["role"], "member")
        self.assertEqual(row["created_by"], self.owner_ctx.account_id)
        # The raw token must not appear anywhere in the table.
        with self.backend.transaction() as tx:
            for r in tx.execute("SELECT * FROM cloud_identity_invites").fetchall():
                for value in r:
                    self.assertNotEqual(value, raw_token,
                                        "raw invite token must never be stored")

    # -- 2. role-scoped: member invite grants member; admin invite grants admin --

    def test_accept_applies_invite_role_exactly(self) -> None:
        _, short_token = invites.create(
            self.owner_ctx, "short-password@example.com", role="member"
        )
        with self.assertRaises(ValueError):
            invites.accept(
                self.backend, short_token, "short-password@example.com", "short"
            )
        for role in ("member", "admin"):
            invite_id, raw_token = invites.create(
                self.owner_ctx, f"guest-{role}@example.com", role=role
            )
            new_account_id, session_token = invites.accept(
                self.backend, raw_token, f"guest-{role}@example.com", self.PASSWORD
            )
            self.assertTrue(new_account_id.startswith("acct_"))
            self.assertTrue(session_token.startswith("fss_"))
            with self.backend.transaction() as tx:
                row = tx.execute(
                    "SELECT role FROM cloud_identity_members "
                    "WHERE tenant_id = ? AND account_id = ?",
                    (self.tenant_id, new_account_id),
                ).fetchone()
            self.assertEqual(row["role"], role)

    # -- 3. the accepted account can authenticate with the given password --

    def test_accepted_account_can_authenticate(self) -> None:
        from weft_cloud.identity import accounts

        _, raw_token = invites.create(self.owner_ctx, "guest@example.com", role="member")
        new_account_id, _ = invites.accept(
            self.backend, raw_token, "guest@example.com", self.PASSWORD
        )
        authenticated = accounts.authenticate(
            self.backend, self.tenant_id, "guest@example.com", self.PASSWORD
        )
        self.assertEqual(authenticated, new_account_id)

    def test_accept_migrates_existing_member_and_rotates_legacy_credentials(self) -> None:
        """An old bootstrap member can adopt a private password via invite."""
        from weft_cloud.identity import accounts

        legacy_password = "legacy-bootstrap-password"
        private_password = "private-password-after-invite"
        account_id = accounts._create_account(
            self.backend,
            self.tenant_id,
            "legacy@example.com",
            legacy_password,
            email_verified=1,
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'member', datetime('now'))",
                (self.tenant_id, account_id),
            )
            tx.commit()
        _, old_session = sessions.create(
            self.backend, self.tenant_id, account_id, role="member"
        )

        _, invite_token = invites.create(
            self.owner_ctx, "legacy@example.com", role="member"
        )
        migrated_id, _ = invites.accept(
            self.backend, invite_token, "legacy@example.com", private_password
        )

        self.assertEqual(migrated_id, account_id)
        self.assertEqual(
            accounts.authenticate(
                self.backend, self.tenant_id, "legacy@example.com", private_password
            ),
            account_id,
        )
        with self.assertRaises(accounts.AuthError):
            accounts.authenticate(
                self.backend, self.tenant_id, "legacy@example.com", legacy_password
            )
        with self.assertRaises(sessions.AuthError):
            sessions.validate(self.backend, old_session)

    def test_existing_member_cannot_use_invite_to_change_role(self) -> None:
        """Migration is role-preserving; role changes stay in orgs.set_role."""
        from weft_cloud.identity import accounts

        account_id = accounts._create_account(
            self.backend,
            self.tenant_id,
            "role-locked@example.com",
            "existing-password",
            email_verified=1,
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'member', datetime('now'))",
                (self.tenant_id, account_id),
            )
            tx.commit()
        _, invite_token = invites.create(
            self.owner_ctx, "role-locked@example.com", role="admin"
        )

        with self.assertRaises(AuthError) as exc:
            invites.accept(
                self.backend, invite_token, "role-locked@example.com", "new-password"
            )
        self.assertEqual(exc.exception.code, "already_in_org")
        self.assertEqual(
            accounts.authenticate(
                self.backend,
                self.tenant_id,
                "role-locked@example.com",
                "existing-password",
            ),
            account_id,
        )

    # -- 4. wrong email refused with invite_mismatch --

    def test_accept_wrong_email_raises_invite_mismatch(self) -> None:
        _, raw_token = invites.create(self.owner_ctx, "guest@example.com", role="member")
        with self.assertRaises(AuthError) as ctx:
            invites.accept(
                self.backend, raw_token, "someone-else@example.com", self.PASSWORD
            )
        self.assertEqual(ctx.exception.code, "invite_mismatch")

    # -- 5. accept twice refused with invite_consumed --

    def test_accept_twice_raises_invite_consumed(self) -> None:
        _, raw_token = invites.create(self.owner_ctx, "guest@example.com", role="member")
        invites.accept(self.backend, raw_token, "guest@example.com", self.PASSWORD)
        with self.assertRaises(AuthError) as ctx:
            invites.accept(
                self.backend, raw_token, "guest@example.com", "Another-Password-!11"
            )
        self.assertEqual(ctx.exception.code, "invite_consumed")

    # -- 6. expired invite refused with invite_expired --

    def test_accept_expired_invite_raises_invite_expired(self) -> None:
        _, raw_token = invites.create(self.owner_ctx, "guest@example.com", role="member")
        with self.backend.transaction() as tx:
            tx.execute(
                "UPDATE cloud_identity_invites SET expires_at = 0.0 "
                "WHERE tenant_id = ? AND email = ?",
                (self.tenant_id, "guest@example.com"),
            )
            tx.commit()
        with self.assertRaises(AuthError) as ctx:
            invites.accept(
                self.backend, raw_token, "guest@example.com", self.PASSWORD
            )
        self.assertEqual(ctx.exception.code, "invite_expired")

    # -- 7. unknown/garbage token refused uniformly (no token oracle) --

    def test_accept_unknown_token_raises_invite_expired(self) -> None:
        garbage = "fiv_" + "A" * 40
        with self.assertRaises(AuthError) as ctx:
            invites.accept(self.backend, garbage, "guest@example.com", self.PASSWORD)
        self.assertEqual(ctx.exception.code, "invite_expired")

    # -- 8. single-use is enforced at the DB row: consumed_at set once --

    def test_accept_sets_consumed_at_once(self) -> None:
        _, raw_token = invites.create(self.owner_ctx, "guest@example.com", role="member")
        invites.accept(self.backend, raw_token, "guest@example.com", self.PASSWORD)
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT consumed_at FROM cloud_identity_invites "
                "WHERE tenant_id = ? AND email = ?",
                (self.tenant_id, "guest@example.com"),
            ).fetchone()
        self.assertIsNotNone(row["consumed_at"])

    # -- 9. member cannot create invites (admin/owner only) --

    def test_member_create_invite_raises_forbidden(self) -> None:
        from weft_cloud.identity import accounts

        member_id, _ = accounts.signup(
            self.backend, self.tenant_id, "member@example.com", self.PASSWORD
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'member', datetime('now'))",
                (self.tenant_id, member_id),
            )
            tx.commit()
        _, member_token = sessions.create(
            self.backend, self.tenant_id, member_id, role="member"
        )
        member_ctx = sessions.validate(self.backend, member_token)

        with self.assertRaises(RoleError) as ctx:
            invites.create(member_ctx, "guest@example.com", role="member")
        self.assertEqual(ctx.exception.code, "forbidden")

    # -- 10. no cross-org: accepted member lands in the invite's tenant --

    def test_accept_lands_in_invite_tenant_only(self) -> None:
        tenant_b = _new_id("tenant")
        from weft_cloud.identity import accounts

        backend = self.backend
        owner_b_ctx = _seed_owner(backend, tenant_b, "owner-b@example.com", self.PASSWORD)

        _, raw_token = invites.create(owner_b_ctx, "joiner@example.com", role="member")
        new_account_id, _ = invites.accept(
            backend, raw_token, "joiner@example.com", self.PASSWORD
        )
        with backend.transaction() as tx:
            row = tx.execute(
                "SELECT tenant_id FROM cloud_identity_members WHERE account_id = ?",
                (new_account_id,),
            ).fetchone()
        self.assertEqual(row["tenant_id"], tenant_b)

        # Org A's owner must not see the joiner who landed in org B.
        members_a = orgs.list_members(self.owner_ctx)
        member_ids = {m["account_id"] for m in members_a}
        self.assertNotIn(new_account_id, member_ids)

    # -- 11. defence in depth: fabricated ctx role cannot bypass the gate --

    def test_fabricated_ctx_role_cannot_create_invite(self) -> None:
        """Layer-2 guard: even a SessionContext hand-built with role='owner'
        is refused when the DB membership is only 'member'. The role gate
        re-derives the actor's role from cloud_identity_members."""
        from weft_cloud.identity import accounts

        member_id, _ = accounts.signup(
            self.backend, self.tenant_id, "lowpriv@example.com", self.PASSWORD
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'member', datetime('now'))",
                (self.tenant_id, member_id),
            )
            tx.commit()

        # Hand-built context claiming 'owner' — must NOT satisfy the gate.
        forged_ctx = SessionContext(
            tenant_id=self.tenant_id,
            account_id=member_id,
            role="owner",
            backend=self.backend,
        )
        with self.assertRaises(RoleError) as ctx:
            invites.create(forged_ctx, "guest@example.com", role="member")
        self.assertEqual(ctx.exception.code, "forbidden")

    # -- 12. the invite email is enqueued via the outbox with the raw token --

    def test_create_enqueues_invite_email_with_raw_token(self) -> None:
        _, raw_token = invites.create(self.owner_ctx, "guest@example.com", role="member")
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? "
                "ORDER BY created_at DESC LIMIT 1",
                ("guest@example.com",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn(raw_token, row["body"])

    def test_invite_email_contains_configured_clickable_web_url(self) -> None:
        previous = os.environ.get("WEFT_WEB_PUBLIC_ORIGIN")
        os.environ["WEFT_WEB_PUBLIC_ORIGIN"] = "https://app.example.test/"
        try:
            _, raw_token = invites.create(self.owner_ctx, "clickable@example.com", role="member")
        finally:
            if previous is None:
                os.environ.pop("WEFT_WEB_PUBLIC_ORIGIN", None)
            else:
                os.environ["WEFT_WEB_PUBLIC_ORIGIN"] = previous

        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? "
                "ORDER BY created_at DESC LIMIT 1",
                ("clickable@example.com",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn(
            f"https://app.example.test/invite/{raw_token}",
            row["body"],
        )

    def test_invalid_web_origin_refuses_before_writing_invite(self) -> None:
        previous = os.environ.get("WEFT_WEB_PUBLIC_ORIGIN")
        invalid_values = (
            "javascript:alert(1)",
            "https://user:pass@example.test",
            "https://example.test/path",
            "https://example.test/?next=evil",
            "https://example.test/#fragment",
            "https://example.test\r\nX-Leak: yes",
        )
        try:
            for index, value in enumerate(invalid_values):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    os.environ["WEFT_WEB_PUBLIC_ORIGIN"] = value
                    invites.create(
                        self.owner_ctx,
                        f"invalid-origin-{index}@example.com",
                        role="member",
                    )
        finally:
            if previous is None:
                os.environ.pop("WEFT_WEB_PUBLIC_ORIGIN", None)
            else:
                os.environ["WEFT_WEB_PUBLIC_ORIGIN"] = previous

        with self.backend.transaction() as tx:
            count = tx.execute(
                "SELECT COUNT(*) AS n FROM cloud_identity_outbox "
                "WHERE tenant_id = ? AND to_email LIKE 'invalid-origin-%'",
                (self.tenant_id,),
            ).fetchone()["n"]
        self.assertEqual(count, 0)

    def test_blank_web_origin_falls_back_to_public_origin(self) -> None:
        previous_web = os.environ.get("WEFT_WEB_PUBLIC_ORIGIN")
        previous_public = os.environ.get("WEFT_PUBLIC_ORIGIN")
        os.environ["WEFT_WEB_PUBLIC_ORIGIN"] = "   "
        os.environ["WEFT_PUBLIC_ORIGIN"] = "https://fallback.example.test/"
        try:
            _, raw_token = invites.create(
                self.owner_ctx, "fallback-origin@example.com", role="member"
            )
        finally:
            if previous_web is None:
                os.environ.pop("WEFT_WEB_PUBLIC_ORIGIN", None)
            else:
                os.environ["WEFT_WEB_PUBLIC_ORIGIN"] = previous_web
            if previous_public is None:
                os.environ.pop("WEFT_PUBLIC_ORIGIN", None)
            else:
                os.environ["WEFT_PUBLIC_ORIGIN"] = previous_public

        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? "
                "ORDER BY created_at DESC LIMIT 1",
                ("fallback-origin@example.com",),
            ).fetchone()
        self.assertIn(
            f"https://fallback.example.test/invite/{raw_token}",
            row["body"],
        )

    # -- 13. migration cloud_005_identity_invites is recorded --

    def test_migration_cloud_005_identity_invites_recorded(self) -> None:
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT migration_id FROM schema_migrations WHERE migration_id = ?",
                ("cloud_005_identity_invites",),
            ).fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
