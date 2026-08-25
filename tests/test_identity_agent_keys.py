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
from tests._server_readiness import await_serving as _await_serving

import hashlib
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.storage import StorageBackend, SqliteWalBackend

# The agent-keys module does not exist yet — this import is the RED gate.
from weft_cloud.identity import accounts, agent_keys, orgs, sessions
from weft_cloud.identity.context import SessionContext, RoleError
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.identity.tokens import AuthError
from weft_cloud.service import WeftCloudService, _CloudHTTPHandler


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

    def test_org_removal_permanently_revokes_keys_and_preserves_other_tenant(self) -> None:
        """Admin removal kills old keys even if membership is later restored."""
        member_id = accounts._create_account(
            self.backend, self.tenant_id, "removed-key@example.com",
            "correct horse battery staple", email_verified=1,
        )
        tenant_b = "tenant_other_for_remove"
        self.backend.create_tenant(tenant_b, "Other Org")
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'member', ?)",
                (self.tenant_id, member_id, "now"),
            )
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'member', ?)",
                (tenant_b, member_id, "now"),
            )
            tx.commit()

        owner_session, owner_raw = sessions.create(
            self.backend, self.tenant_id, self.account_id, role="owner"
        )
        self.assertIsNotNone(owner_session)
        owner_ctx = sessions.validate(self.backend, owner_raw)
        _key_a, raw_a = agent_keys.create(
            self.backend, self.tenant_id, member_id, "tenant-a"
        )
        _key_b, raw_b = agent_keys.create(self.backend, tenant_b, member_id, "tenant-b")
        self.assertEqual(agent_keys.validate(self.backend, raw_a).tenant_id, self.tenant_id)
        self.assertEqual(agent_keys.validate(self.backend, raw_b).tenant_id, tenant_b)

        orgs.remove_member(owner_ctx, member_id)
        with self.assertRaises(AuthError) as removed:
            agent_keys.validate(self.backend, raw_a)
        self.assertEqual(removed.exception.args[0], "invalid_session")
        self.assertEqual(agent_keys.validate(self.backend, raw_b).tenant_id, tenant_b)

        # Re-adding the account must not resurrect the old tenant-A key.
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'member', ?)",
                (self.tenant_id, member_id, "later"),
            )
            tx.commit()
        with self.assertRaises(AuthError) as readded:
            agent_keys.validate(self.backend, raw_a)
        self.assertEqual(readded.exception.args[0], "invalid_session")
        self.assertEqual(agent_keys.validate(self.backend, raw_b).tenant_id, tenant_b)

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


class _SignoutHTTPHarness:
    """Real HTTP server + WeftCloudService for the signout regression."""

    def __init__(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="agk-signout-")
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{port}"
        self.service = WeftCloudService(
            SqliteWalBackend(str(Path(self._tmp) / "srv.db")), origin=self.base
        )
        _CloudHTTPHandler.service = self.service
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        _await_serving(self._httpd)

    def request(self, method: str, path: str, body=None, token: str | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                return exc.code, json.loads(raw.decode("utf-8")) if raw else {}
            finally:
                exc.close()

    def close(self) -> None:
        try:
            self._httpd.shutdown()
        finally:
            self._httpd.server_close()
        try:
            self.service.backend.close()
        except Exception:
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)


class SignoutWithAgentKeyRegressionTests(unittest.TestCase):
    """POST /v1/auth/signout accepts BOTH credential types through the shared
    auth funnel (``_authenticate``), but revokes only
    ``cloud_identity_sessions``. For an ``agk_`` bearer the endpoint therefore
    answers ``{"signed_out": true}`` while the presented credential stays fully
    live — a silent failure of the revocation surface.

    Invariant under test: when signout answers success, the presented
    credential must no longer authenticate; otherwise the request must be
    refused. Either behaviour is safe; silent success is not.
    """

    def setUp(self) -> None:
        self.harness = _SignoutHTTPHarness()

    def tearDown(self) -> None:
        self.harness.close()

    def test_signout_with_agent_key_never_claims_success_while_key_lives(self) -> None:
        status, body = self.harness.request(
            "POST", "/v1/auth/signup",
            {"email": f"sig{time.time_ns()}@example.com", "password": "CorrectHorse!1"},
        )
        self.assertEqual(status, 201, f"signup failed: {body}")
        session = body["session_token"]

        status, created = self.harness.request(
            "POST", "/v1/agent-keys", {"label": "signout-probe"}, token=session
        )
        self.assertEqual(status, 201, f"agent-key create failed: {created}")
        agent_key = created["agent_key"]

        # Sanity: the key authenticates before signout.
        status, me = self.harness.request("GET", "/v1/me", token=agent_key)
        self.assertEqual(status, 200, f"key must authenticate before signout: {me}")

        status, out = self.harness.request("POST", "/v1/auth/signout", {}, token=agent_key)

        if status == 200 and out.get("signed_out"):
            # Success was claimed — the credential MUST be dead.
            status_after, me_after = self.harness.request("GET", "/v1/me", token=agent_key)
            self.assertEqual(
                status_after,
                401,
                "signout reported signed_out: true but the agent key still "
                f"authenticates (/v1/me returned {status_after}: {me_after}) — "
                "a silently-live credential",
            )
        else:
            # The alternative safe behaviour: refuse agent keys at signout.
            self.assertEqual(
                status,
                401,
                "refusing agent keys at signout must be a 401, not a silent "
                f"non-success (got {status})",
            )


if __name__ == "__main__":
    unittest.main()
