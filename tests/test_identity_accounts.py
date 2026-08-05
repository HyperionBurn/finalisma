"""Wave G — Identity: accounts contract (RED).

Drives the accounts module through the real identity API against a real
SqliteWalBackend on a temp file. The ``finalisma_cloud.identity`` package does
not exist yet, so the import fails — that ModuleNotFoundError is the deliverable.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 3, 5, 9.1, 12.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finalisma_cloud.identity import accounts  # noqa: E402 — RED: package absent
from finalisma_cloud.identity.mailer import LocalOutboxMailer  # noqa: E402
from finalisma_cloud.migrations import apply_migrations  # noqa: E402
from finalisma_cloud.storage import SqliteWalBackend  # noqa: E402


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
    # Keep the tempdir alive for the lifetime of the backend; tests that need
    # teardown can't rely on __del__, so we attach it and let the process exit
    # clean it up. (The project's own integration tests use TemporaryDirectory
    # in setUp/tearDown; this seam keeps the helper self-contained.)
    backend._tmpdir = tmp  # type: ignore[attr-defined]
    return backend


class IdentityAccountsContractTests(unittest.TestCase):
    """Accounts contract through the real identity API (design section 3)."""

    def setUp(self) -> None:
        self.backend = make_backend()
        self.tenant_id = _new_id("tenant")
        # signup creates the tenant if absent; these tests use that path.
        self.email = "alice@example.com"
        self.password = "correct-horse-battery-staple"

    def tearDown(self) -> None:
        try:
            self.backend.close()
        finally:
            tmp = getattr(self.backend, "_tmpdir", None)
            if tmp is not None:
                tmp.cleanup()

    # -- 1. signup creates account; email_verified=0; raw password not returned --

    def test_signup_creates_unverified_account_and_returns_token(self) -> None:
        account_id, verification_token = accounts.signup(
            self.backend, self.tenant_id, self.email, self.password
        )
        self.assertTrue(account_id.startswith("acct_"))
        self.assertTrue(verification_token.startswith("fvt_"))

        # Account row exists, unverified, and the raw password is NOT stored.
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT account_id, email, email_verified, password_hash, salt "
                "FROM cloud_identity_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["email"], self.email)
        self.assertEqual(row["email_verified"], 0)
        self.assertIsNotNone(row["password_hash"])
        self.assertIsNotNone(row["salt"])
        self.assertNotIn(
            self.password, (row["password_hash"], row["salt"])
        )

    # -- 2. verify_email sets verified; second use raises (single-use) --

    def test_verify_email_sets_verified_and_is_single_use(self) -> None:
        _, verification_token = accounts.signup(
            self.backend, self.tenant_id, self.email, self.password
        )
        # First use succeeds.
        accounts.verify_email(self.backend, verification_token)
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT email_verified FROM cloud_identity_accounts WHERE email = ?",
                (self.email,),
            ).fetchone()
        self.assertEqual(row["email_verified"], 1)

        # Second use with the same token raises invalid_token.
        with self.assertRaises(accounts.AuthError) as ctx:
            accounts.verify_email(self.backend, verification_token)
        self.assertEqual(str(ctx.exception), "invalid_token")

    # -- 3. authenticate with correct password returns account_id --

    def test_authenticate_correct_password_returns_account_id(self) -> None:
        account_id, _ = accounts.signup(
            self.backend, self.tenant_id, self.email, self.password
        )
        result = accounts.authenticate(
            self.backend, self.tenant_id, self.email, self.password
        )
        self.assertEqual(result, account_id)

    # -- 4. authenticate with wrong password raises invalid_credentials --

    def test_authenticate_wrong_password_raises_invalid_credentials(self) -> None:
        accounts.signup(self.backend, self.tenant_id, self.email, self.password)
        with self.assertRaises(accounts.AuthError) as ctx:
            accounts.authenticate(
                self.backend, self.tenant_id, self.email, "wrong-password"
            )
        self.assertEqual(str(ctx.exception), "invalid_credentials")

    # -- 5. unknown email raises invalid_credentials; message reveals nothing --

    def test_authenticate_unknown_email_raises_without_leaking_existence(self) -> None:
        with self.assertRaises(accounts.AuthError) as ctx:
            accounts.authenticate(
                self.backend, self.tenant_id, "nobody@example.com", self.password
            )
        self.assertEqual(str(ctx.exception), "invalid_credentials")
        # The error message must not reveal whether the email exists.
        self.assertNotIn(self.email, str(ctx.exception))
        self.assertNotIn("nobody@example.com", str(ctx.exception))
        self.assertNotIn(self.password, str(ctx.exception))

    # -- 6. reset_password with valid token changes the password --

    def test_reset_password_valid_token_changes_password(self) -> None:
        accounts.signup(self.backend, self.tenant_id, self.email, self.password)
        accounts.request_password_reset(self.backend, self.tenant_id, self.email)
        # Recover the raw reset token from the outbox (Stage 1 mailer writes it).
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (self.email,),
            ).fetchone()
        # The reset email body contains the raw frt_ token.
        reset_token = self._extract_reset_token(row["body"])
        self.assertTrue(reset_token.startswith("frt_"))

        new_password = "new-secure-password-42"
        accounts.reset_password(self.backend, reset_token, new_password)

        # Old password no longer authenticates; new one does.
        with self.assertRaises(accounts.AuthError):
            accounts.authenticate(
                self.backend, self.tenant_id, self.email, self.password
            )
        account_id = accounts.authenticate(
            self.backend, self.tenant_id, self.email, new_password
        )
        self.assertTrue(account_id.startswith("acct_"))

    # -- 7. reset_password with already-consumed token raises invalid_token --

    def test_reset_password_consumed_token_raises_invalid_token(self) -> None:
        accounts.signup(self.backend, self.tenant_id, self.email, self.password)
        accounts.request_password_reset(self.backend, self.tenant_id, self.email)
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (self.email,),
            ).fetchone()
        reset_token = self._extract_reset_token(row["body"])

        accounts.reset_password(self.backend, reset_token, "first-new-password")
        # Second use of the same token must fail.
        with self.assertRaises(accounts.AuthError) as ctx:
            accounts.reset_password(self.backend, reset_token, "second-new-password")
        self.assertEqual(str(ctx.exception), "invalid_token")

    # -- 8. verify_email with a tampered token raises invalid_token --

    def test_verify_email_tampered_token_raises_invalid_token(self) -> None:
        tampered = "fvt_" + "A" * 40  # valid prefix, garbage digest
        with self.assertRaises(accounts.AuthError) as ctx:
            accounts.verify_email(self.backend, tampered)
        self.assertEqual(str(ctx.exception), "invalid_token")

    # -- 9. LocalOutboxMailer writes an entry to cloud_identity_outbox --

    def test_local_outbox_mailer_writes_outbox_row(self) -> None:
        mailer = LocalOutboxMailer(self.backend)
        mailer.send(
            self.tenant_id,
            "to@example.com",
            "Verify your email",
            "Click the link: fvt_abc123",
        )
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT tenant_id, to_email, subject, body FROM cloud_identity_outbox "
                "WHERE to_email = ?",
                ("to@example.com",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["tenant_id"], self.tenant_id)
        self.assertEqual(row["to_email"], "to@example.com")
        self.assertEqual(row["subject"], "Verify your email")

    # -- 10. schema_migrations records cloud_002_identity_accounts --

    def test_migration_cloud_002_identity_accounts_recorded(self) -> None:
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT migration_id FROM schema_migrations WHERE migration_id = ?",
                ("cloud_002_identity_accounts",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["migration_id"], "cloud_002_identity_accounts")

    # -- helpers --

    @staticmethod
    def _extract_reset_token(body: str) -> str:
        """Pull the raw frt_ token out of the reset email body."""
        for token in body.split():
            if token.startswith("frt_"):
                return token
        raise AssertionError(f"no frt_ token found in reset email body: {body!r}")
