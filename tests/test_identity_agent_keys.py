"""Agent API keys — long-lived, revocable credential store contract (TDD — RED).

These tests exercise the Wave H agent-key contract through the real identity
API surface (``weft_cloud.identity.agent_keys``), which does not exist yet —
this file must FAIL at import time.

Agent keys are the config-file credential for agent-to-agent products: a
desktop MCP client holds a static bearer token and never signs in, so the
24-hour session clock silently kills every connector every day. An agent key
is long-lived (no expiry clock), stored as SHA-256 only, shown raw EXACTLY
once, immediately revocable, scoped to the creating tenant + account, and its
role is RE-DERIVED from membership at validation time so it can never carry
more than the account currently holds.

Per AGENTS.md: no mocks for the SQLite layer. make_backend() returns a real
SqliteWalBackend on a temp file.

Authoritative spec: docs/AGENT_KEYS.md.
"""

from __future__ import annotations

import hashlib
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.storage import StorageBackend, SqliteWalBackend

# The agent-keys module does not exist yet — this import is the RED gate.
from weft_cloud.identity import accounts, agent_keys
from weft_cloud.identity.context import SessionContext, RoleError
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.identity.tokens import AuthError


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_backend() -> StorageBackend:
    """Factory seam — returns a fresh SqliteWalBackend on a unique temp file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    return SqliteWalBackend(path)


class AgentKeyContractTests(unittest.TestCase):
    """Agent-key lifecycle + token hygiene + role re-derivation contract."""

    def setUp(self) -> None:
        self.backend = make_backend()
        self.backend.initialize()
        ensure_schema(self.backend)
        self.tenant_id = "tenant_test"
        self.backend.create_tenant(self.tenant_id, "Test Org")
        self.account_id, _vt = accounts.signup(
            self.backend, self.tenant_id, "alice@example.com", "correct horse battery staple"
        )
        self._membership("owner")

    def tearDown(self) -> None:
        if hasattr(self.backend, "close"):
            self.backend.close()

    def _membership(self, role: str) -> None:
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(tenant_id, account_id) DO UPDATE SET role = ?",
                (self.tenant_id, self.account_id, role, "now", role),
            )
            tx.commit()

    # --- 1. create returns raw agk_ token ONCE; only sha256 stored ---
    def test_create_returns_raw_key_once_and_never_stores_it(self) -> None:
        key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "ci")
        self.assertTrue(raw_token.startswith("agk_"))
        self.assertIsInstance(key_id, str)
        self.assertTrue(len(key_id) > 0)

        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT token_hash FROM cloud_identity_agent_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["token_hash"], _sha256(raw_token))
            all_rows = tx.execute("SELECT * FROM cloud_identity_agent_keys").fetchall()
            for r in all_rows:
                for value in r:
                    self.assertNotEqual(
                        value, raw_token,
                        "raw agent key must never be stored in the DB",
                    )

    # --- 2. validate resolves the SAME SessionContext shape with membership role ---
    def test_validate_returns_session_context_with_membership_role(self) -> None:
        _key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "ci")
        ctx = agent_keys.validate(self.backend, raw_token)
        self.assertIsInstance(ctx, SessionContext)
        self.assertEqual(ctx.tenant_id, self.tenant_id)
        self.assertEqual(ctx.account_id, self.account_id)
        self.assertEqual(ctx.role, "owner")
        self.assertIs(ctx.backend, self.backend)

    # --- 3. unknown, tampered, and revoked keys refuse identically (no oracle) ---
    def test_unknown_tampered_revoked_all_raise_identical_invalid_session(self) -> None:
        _key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "ci")
        tampered = raw_token[:-1] + ("X" if raw_token[-1] != "X" else "Y")

        def code(tok: str) -> str:
            with self.assertRaises(AuthError) as cm:
                agent_keys.validate(self.backend, tok)
            return cm.exception.args[0]

        self.assertEqual(code("agk_totally-unknown-token"), "invalid_session")
        self.assertEqual(code(tampered), "invalid_session")
        agent_keys.revoke(self.backend, self.tenant_id, self.account_id, _key_id)
        self.assertEqual(code(raw_token), "invalid_session")

    # --- 4. revoked key refused on the very next request ---
    def test_revoked_key_immediately_refused(self) -> None:
        key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "ci")
        self.assertIsInstance(agent_keys.validate(self.backend, raw_token), SessionContext)
        agent_keys.revoke(self.backend, self.tenant_id, self.account_id, key_id)
        with self.assertRaises(AuthError) as cm:
            agent_keys.validate(self.backend, raw_token)
        self.assertEqual(cm.exception.args[0], "invalid_session")

    # --- 5. long-lived: no expiry clock at all ---
    def test_key_has_no_expiry_clock(self) -> None:
        key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "ci")
        with self.backend.transaction() as tx:
            cols = {r["name"] for r in tx.execute(
                "PRAGMA table_info('cloud_identity_agent_keys')").fetchall()}
        self.assertNotIn("expires_at", cols, "agent keys must have no expiry column")
        # A key created seconds ago and validated later (well past a 24h TTL in
        # session terms) still resolves — there is no clock to expire it.
        time.sleep(1.0)
        ctx = agent_keys.validate(self.backend, raw_token)
        self.assertEqual(ctx.account_id, self.account_id)
        self.assertIsNotNone(key_id)

    # --- 6. never grants more than the account currently holds (role re-derived) ---
    def test_demotion_is_reflected_on_next_request(self) -> None:
        _key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "ci")
        ctx = agent_keys.validate(self.backend, raw_token)
        self.assertEqual(ctx.role, "owner")

        self._membership("member")
        demoted = agent_keys.validate(self.backend, raw_token)
        self.assertEqual(demoted.role, "member")
        with self.assertRaises(RoleError) as cm:
            demoted.require_role("admin")
        self.assertEqual(cm.exception.args[0], "forbidden")

    # --- 7. a removed membership refuses the key (account no longer in tenant) ---
    def test_removed_membership_refuses_key(self) -> None:
        _key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "ci")
        with self.backend.transaction() as tx:
            tx.execute(
                "DELETE FROM cloud_identity_members WHERE tenant_id = ? AND account_id = ?",
                (self.tenant_id, self.account_id),
            )
            tx.commit()
        with self.assertRaises(AuthError):
            agent_keys.validate(self.backend, raw_token)

    # --- 8. list shows metadata, never the secret or its hash ---
    def test_list_returns_metadata_never_secret(self) -> None:
        key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "prod-agent")
        # Touch last_used.
        agent_keys.validate(self.backend, raw_token)
        keys = agent_keys.list_for_account(self.backend, self.tenant_id, self.account_id)
        self.assertEqual(len(keys), 1)
        entry = keys[0]
        self.assertEqual(entry["key_id"], key_id)
        self.assertEqual(entry["label"], "prod-agent")
        self.assertIsNotNone(entry["created_at"])
        self.assertIsNotNone(entry["last_used_at"])
        self.assertIsNone(entry["revoked_at"])
        for k, v in entry.items():
            self.assertNotIn("agk_", str(v), "list must never leak the raw key")
            self.assertNotEqual(v, raw_token)
            self.assertNotEqual(v, _sha256(raw_token))

    # --- 9. revoke is scoped: an account cannot revoke another's key ---
    def test_revoke_is_scoped_to_owning_account(self) -> None:
        _key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "mine")
        other_id, _ = accounts.signup(
            self.backend, "tenant_other", "bob@example.com", "correct horse battery staple"
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES ('tenant_other', ?, 'owner', 'now')",
                (other_id,),
            )
            tx.commit()
        other_key_id, other_token = agent_keys.create(
            self.backend, "tenant_other", other_id, "theirs"
        )
        # Alice (owning the first key) cannot revoke Bob's key from a different
        # tenant+account: the conditional UPDATE matches nothing.
        agent_keys.revoke(self.backend, self.tenant_id, self.account_id, other_key_id)
        self.assertIsInstance(agent_keys.validate(self.backend, other_token), SessionContext)

    # --- 10. revoke_all_for_account kills every key of the account ---
    def test_revoke_all_for_account_kills_every_key(self) -> None:
        _k1, t1 = agent_keys.create(self.backend, self.tenant_id, self.account_id, "a")
        _k2, t2 = agent_keys.create(self.backend, self.tenant_id, self.account_id, "b")
        agent_keys.revoke_all_for_account(self.backend, self.account_id)
        for tok in (t1, t2):
            with self.assertRaises(AuthError):
                agent_keys.validate(self.backend, tok)

    # --- 11. reset_password also revokes the account's agent keys ---
    def test_reset_password_revokes_agent_keys(self) -> None:
        _key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "ci")
        accounts.request_password_reset(self.backend, self.tenant_id, "alice@example.com")
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? "
                "ORDER BY created_at DESC LIMIT 1",
                ("alice@example.com",),
            ).fetchone()
        self.assertIsNotNone(row)
        match = re.search(r"(frt_[A-Za-z0-9_-]+)", row["body"])
        self.assertIsNotNone(match)
        accounts.reset_password(self.backend, match.group(1), "a-brand-new-password-1")
        with self.assertRaises(AuthError) as cm:
            agent_keys.validate(self.backend, raw_token)
        self.assertEqual(cm.exception.args[0], "invalid_session")

    # --- 12. validate records last_used_at ---
    def test_validate_records_last_used(self) -> None:
        key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "ci")
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT last_used_at FROM cloud_identity_agent_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
            self.assertIsNone(row["last_used_at"])
        agent_keys.validate(self.backend, raw_token)
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT last_used_at FROM cloud_identity_agent_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
            self.assertIsNotNone(row["last_used_at"])

    # --- 13. SessionContext repr never contains the raw key ---
    def test_context_repr_never_contains_raw_key(self) -> None:
        _key_id, raw_token = agent_keys.create(self.backend, self.tenant_id, self.account_id, "ci")
        ctx = agent_keys.validate(self.backend, raw_token)
        self.assertNotIn("agk_", repr(ctx))


if __name__ == "__main__":
    unittest.main()
