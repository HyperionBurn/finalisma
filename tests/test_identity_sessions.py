"""Identity sessions integration tests (TDD — RED deliverable).

These tests exercise the Wave G session contract through the real identity
API (finalisma_cloud.identity). The identity modules do not exist yet, so
this file must FAIL at import time.

Per AGENTS.md: no mocks for the SQLite layer. make_backend() returns a real
SqliteWalBackend on a temp file. Every test drives the real storage seam.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 4, 8, 9.3.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finalisma_cloud.storage import StorageBackend, SqliteWalBackend

# The identity package does not exist yet — this import is the RED gate.
from finalisma_cloud.identity import accounts, sessions
from finalisma_cloud.identity.context import SessionContext, RoleError
from finalisma_cloud.identity.tokens import AuthError


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_backend() -> StorageBackend:
    """Factory seam — returns a fresh SqliteWalBackend on a unique temp file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    return SqliteWalBackend(path)


class SessionContractTests(unittest.TestCase):
    """Session lifecycle + token-hygiene contract.

    Every test creates a tenant + account first (through the real identity
    API), then exercises sessions.create / validate / revoke.
    """

    def setUp(self) -> None:
        self.backend = make_backend()
        self.backend.initialize()
        # Pre-apply identity tables through the real identity API surface.
        # The identity package exposes ensure_schema() which runs migrations
        # cloud_002..cloud_006 on the backend. If that entry point is not
        # the final name, the orchestrator must wire it.
        try:
            from finalisma_cloud.identity import ensure_schema
            ensure_schema(self.backend)
        except ImportError:
            # Fallback: apply migrations directly via the backend so tests
            # can still run once accounts/sessions exist.
            self._apply_identity_migrations()
        self.tenant_id = "tenant_test"
        self.backend.create_tenant(self.tenant_id, "Test Org")
        self.account_id, self.verification_token = accounts.signup(
            self.backend, self.tenant_id, "alice@example.com", "correct horse battery staple"
        )

    def tearDown(self) -> None:
        if hasattr(self.backend, "close"):
            self.backend.close()

    def _apply_identity_migrations(self) -> None:
        """Minimal identity-table setup so the suite can run once the
        identity package exists but before ensure_schema() is named."""
        with self.backend.transaction() as tx:
            tx.execute("""
                CREATE TABLE IF NOT EXISTS cloud_identity_accounts (
                    account_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    email_verified INTEGER NOT NULL DEFAULT 0,
                    verification_token_hash TEXT,
                    verification_expires_at REAL NOT NULL DEFAULT 0,
                    reset_token_hash TEXT,
                    reset_expires_at REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY(tenant_id) REFERENCES cloud_tenants(tenant_id)
                )
            """)
            tx.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_accounts_tenant_email_unique
                ON cloud_identity_accounts(tenant_id, email)
            """)
            tx.execute("""
                CREATE TABLE IF NOT EXISTS cloud_identity_sessions (
                    session_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL,
                    role_snapshot TEXT NOT NULL,
                    FOREIGN KEY(tenant_id) REFERENCES cloud_tenants(tenant_id),
                    FOREIGN KEY(account_id) REFERENCES cloud_identity_accounts(account_id)
                )
            """)
            tx.execute("""
                CREATE INDEX IF NOT EXISTS idx_identity_sessions_account
                ON cloud_identity_sessions(account_id, revoked_at)
            """)
            tx.execute("""
                CREATE INDEX IF NOT EXISTS idx_identity_sessions_token
                ON cloud_identity_sessions(token_hash)
            """)
            tx.commit()

    # --- 1. create returns raw fss_ token + session_id; raw NOT stored ---
    def test_create_returns_raw_token_and_session_id_raw_not_stored(self) -> None:
        session_id, raw_token = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="member"
        )
        self.assertTrue(raw_token.startswith("fss_"))
        self.assertIsInstance(session_id, str)
        self.assertTrue(len(session_id) > 0)

        # Raw token must NOT appear anywhere in the DB.
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT token_hash FROM cloud_identity_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            stored_hash = row["token_hash"]
            self.assertEqual(stored_hash, _sha256(raw_token))
            # The raw token itself must not be present in any column of the table.
            all_rows = tx.execute("SELECT * FROM cloud_identity_sessions").fetchall()
            for r in all_rows:
                for value in r:
                    self.assertNotEqual(
                        value, raw_token,
                        "raw session token must never be stored in the DB",
                    )

    # --- 2. validate returns SessionContext with correct role; tampered fails ---
    def test_validate_returns_context_with_role_tampered_fails(self) -> None:
        session_id, raw_token = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="admin"
        )
        ctx = sessions.validate(self.backend, raw_token)
        self.assertIsInstance(ctx, SessionContext)
        self.assertEqual(ctx.role, "admin")
        self.assertEqual(ctx.tenant_id, self.tenant_id)
        self.assertEqual(ctx.account_id, self.account_id)
        self.assertIs(ctx.backend, self.backend)

        # Tampered token must raise AuthError("invalid_session").
        tampered = raw_token[:-1] + ("X" if raw_token[-1] != "X" else "Y")
        with self.assertRaises(AuthError) as cm:
            self.backend and sessions.validate(self.backend, tampered)
        self.assertEqual(cm.exception.args[0], "invalid_session")

    # --- 3. revoke(session_id) makes validate fail ---
    def test_revoke_makes_validate_fail(self) -> None:
        session_id, raw_token = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="member"
        )
        # Sanity: valid before revoke.
        ctx = sessions.validate(self.backend, raw_token)
        self.assertEqual(ctx.role, "member")

        sessions.revoke(self.backend, session_id)
        with self.assertRaises(AuthError) as cm:
            sessions.validate(self.backend, raw_token)
        self.assertEqual(cm.exception.args[0], "invalid_session")

    # --- 4. expired session: tiny ttl, sleep past it, validate fails ---
    def test_expired_session_fails_validation(self) -> None:
        session_id, raw_token = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="member", ttl_seconds=1
        )
        time.sleep(1.5)
        with self.assertRaises(AuthError) as cm:
            sessions.validate(self.backend, raw_token)
        self.assertEqual(cm.exception.args[0], "invalid_session")

    # --- 5. revoke_all_for_account revokes every session for the account ---
    def test_revoke_all_for_account_revokes_every_session(self) -> None:
        _, token_a = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="member"
        )
        _, token_b = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="admin"
        )
        # Both valid before.
        self.assertEqual(sessions.validate(self.backend, token_a).role, "member")
        self.assertEqual(sessions.validate(self.backend, token_b).role, "admin")

        sessions.revoke_all_for_account(self.backend, self.account_id)

        with self.assertRaises(AuthError):
            sessions.validate(self.backend, token_a)
        with self.assertRaises(AuthError):
            sessions.validate(self.backend, token_b)

    # --- 6. rotation: revoke-all + re-issue → new role, old token fails ---
    def test_rotation_revoke_all_then_reissue_new_role_old_fails(self) -> None:
        _, old_token = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="member"
        )
        sessions.revoke_all_for_account(self.backend, self.account_id)

        _, new_token = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="admin"
        )
        ctx = sessions.validate(self.backend, new_token)
        self.assertEqual(ctx.role, "admin")

        # Old token still fails.
        with self.assertRaises(AuthError):
            sessions.validate(self.backend, old_token)

    # --- 7. SessionContext.require_role ---
    def test_require_role_raises_for_lower_role_passes_for_equal(self) -> None:
        _, member_token = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="member"
        )
        member_ctx = sessions.validate(self.backend, member_token)
        with self.assertRaises(RoleError) as cm:
            member_ctx.require_role("admin")
        self.assertEqual(cm.exception.args[0], "forbidden")

        # require_role("member") passes for a member session.
        member_ctx.require_role("member")  # must not raise

        # Admin can require admin but not owner.
        _, admin_token = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="admin"
        )
        admin_ctx = sessions.validate(self.backend, admin_token)
        admin_ctx.require_role("admin")
        with self.assertRaises(RoleError):
            admin_ctx.require_role("owner")

    # --- 8. raw token never appears in SessionContext repr ---
    def test_session_context_repr_never_contains_raw_token(self) -> None:
        _, raw_token = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="member"
        )
        ctx = sessions.validate(self.backend, raw_token)
        self.assertNotIn("fss_", repr(ctx))
