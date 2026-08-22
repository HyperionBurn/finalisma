"""Wave G — Identity negative-case contract (RED deliverable).

Authoritative spec: docs/IDENTITY_DESIGN.md section 9 (all 28 negative cases)
and section 10 (no-secrets-in-logs).

This file imports from weft_cloud.identity — which does NOT exist yet.
Every test here encodes a refusal that MUST fire. The refusals ARE the
deliverable. This file must FAIL now with ModuleNotFoundError; when the
identity plane is implemented, each test must pass without weakening.

Test discipline: no mocks for the SQLite layer. make_backend() returns a real
SqliteWalBackend on a temp file, initialized with the cloud schema.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

# Add src/ to sys.path so weft_cloud is importable (mirrors test_tenancy_negative.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# --- identity-plane imports: these modules do NOT exist yet (RED) ---
from weft_cloud.identity.accounts import AccountStore
from weft_cloud.identity.sessions import SessionStore
from weft_cloud.identity.orgs import OrgStore
from weft_cloud.identity.invites import InviteStore
from weft_cloud.identity.context import SessionContext, RoleError
from weft_cloud.identity.tokens import generate_token, hash_token

# --- existing cloud-plane primitives (Wave F) ---
from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_backend() -> SqliteWalBackend:
    """Real SQLite-WAL backend on a temp file, initialized with the cloud schema.

    No mocks — this project tests against real storage. The temp file is
    removed in close().
    """
    tmp = tempfile.NamedTemporaryFile(prefix="id_neg_", suffix=".db", delete=False)
    tmp.close()
    backend = SqliteWalBackend(tmp.name)
    backend.initialize()
    return backend


def _apply_identity_migrations(backend: SqliteWalBackend) -> None:
    """Apply the Wave G identity migrations (cloud_002 .. cloud_006).

    When the identity plane ships, these migrations live in migrations.py.
    For the RED phase we inline the DDL so the tests can build real rows
    against the real backend. The DDL mirrors IDENTITY_DESIGN.md §2.4 exactly.
    """
    ddl = """
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
    );
    CREATE INDEX IF NOT EXISTS idx_identity_accounts_tenant_email
        ON cloud_identity_accounts(tenant_id, email);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_accounts_tenant_email_unique
        ON cloud_identity_accounts(tenant_id, email);

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
    );
    CREATE INDEX IF NOT EXISTS idx_identity_sessions_account
        ON cloud_identity_sessions(account_id, revoked_at);
    CREATE INDEX IF NOT EXISTS idx_identity_sessions_token
        ON cloud_identity_sessions(token_hash);

    CREATE TABLE IF NOT EXISTS cloud_identity_members (
        tenant_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('owner','admin','member')),
        joined_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, account_id),
        FOREIGN KEY(tenant_id) REFERENCES cloud_tenants(tenant_id),
        FOREIGN KEY(account_id) REFERENCES cloud_identity_accounts(account_id)
    );
    CREATE INDEX IF NOT EXISTS idx_identity_members_account
        ON cloud_identity_members(account_id);

    CREATE TABLE IF NOT EXISTS cloud_identity_invites (
        invite_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        email TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','member')),
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        expires_at REAL NOT NULL,
        consumed_at REAL,
        created_by TEXT NOT NULL,
        FOREIGN KEY(tenant_id) REFERENCES cloud_tenants(tenant_id),
        FOREIGN KEY(created_by) REFERENCES cloud_identity_accounts(account_id)
    );
    CREATE INDEX IF NOT EXISTS idx_identity_invites_tenant
        ON cloud_identity_invites(tenant_id, email);

    CREATE TABLE IF NOT EXISTS cloud_identity_outbox (
        entry_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        to_email TEXT NOT NULL,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        dispatched_at REAL,
        FOREIGN KEY(tenant_id) REFERENCES cloud_tenants(tenant_id)
    );
    """
    with backend.transaction() as tx:
        tx.executescript(ddl)
        tx.commit()


def make_tenant(backend: SqliteWalBackend, tenant_id: str, name: str) -> None:
    backend.create_tenant(tenant_id, name, plan_id="free")


# ---------------------------------------------------------------------------
# Shared test secrets — tracked so the no-secrets-in-logs assertions can
# demand their ABSENCE from errors / responses / source.
# ---------------------------------------------------------------------------

KNOWN_PASSWORD = "Correct-Horse-Battery-Staple-!42"
KNOWN_EMAIL = "alice@corp.com"
UNKNOWN_EMAIL = "nobody@nowhere.invalid"
WRONG_PASSWORD = "totally-wrong-password-999"


class IdentityNegatives(unittest.TestCase):
    """Each test encodes one refusal from IDENTITY_DESIGN.md §9."""

    def setUp(self):
        self.backend = make_backend()
        _apply_identity_migrations(self.backend)
        self.accounts = AccountStore(self.backend)
        self.sessions = SessionStore(self.backend)
        self.orgs = OrgStore(self.backend)
        self.invites = InviteStore(self.backend)

    def tearDown(self):
        # Close + wipe the temp DB.
        self.backend.close()
        try:
            os.unlink(self.backend.db_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 9.1 Authentication refusals
    # ------------------------------------------------------------------

    def test_01_wrong_password_refused(self):
        """#2: wrong password raises AuthError("invalid_credentials")."""
        make_tenant(self.backend, "tenant_a", "Org A")
        self.accounts.signup(self.backend, "tenant_a", KNOWN_EMAIL, KNOWN_PASSWORD)

        with self.assertRaises(Exception) as exc:
            self.accounts.authenticate(self.backend, "tenant_a", KNOWN_EMAIL, WRONG_PASSWORD)
        self.assertEqual(exc.exception.args[0], "invalid_credentials")

    def test_02_unknown_user_timing_indistinguishable(self):
        """#1/#3: unknown-user signin does NOT short-circuit over real HTTP.

        The spec requires the same scrypt computation against a dummy hash for
        unknown emails. The original test called ``accounts.authenticate``
        directly — the internal function the real entry point never reaches, so
        it would pass even if the entire HTTP layer bailed early on a
        tenant-lookup miss (which is exactly what the shipped oracle did).

        This drives ``POST /v1/auth/signin`` over a REAL HTTP server and
        asserts the median known-vs-unknown latency ratio stays below 3× — the
        refusal body/status are identical, only the timing differs.
        """
        from weft_cloud.service import WeftCloudService, _CloudHTTPHandler

        tmpdir = tempfile.mkdtemp(prefix="id-neg-timing-")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        service = None
        try:
            backend = SqliteWalBackend(str(Path(tmpdir) / "timing.db"))
            service = WeftCloudService(backend)
            _CloudHTTPHandler.service = service
            port = httpd.server_address[1]
            base = f"http://127.0.0.1:{port}"
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()

            # Create a known account through the REAL signup entry point.
            self._http_post(base, "/v1/auth/signup",
                            {"email": KNOWN_EMAIL, "password": KNOWN_PASSWORD})

            def _signin_ms(email: str, password: str) -> float:
                t0 = time.perf_counter()
                status, _ = self._http_post(base, "/v1/auth/signin",
                                            {"email": email, "password": password})
                self.assertEqual(status, 401)
                return (time.perf_counter() - t0) * 1000.0

            # Warm up both paths (connection + scrypt caches settle).
            for _ in range(3):
                _signin_ms(KNOWN_EMAIL, WRONG_PASSWORD)
                _signin_ms(UNKNOWN_EMAIL, "irrelevant")

            trials = 12
            unknown_times: list[float] = []
            wrong_pw_times: list[float] = []
            for _ in range(trials):
                wrong_pw_times.append(_signin_ms(KNOWN_EMAIL, WRONG_PASSWORD))
                unknown_times.append(_signin_ms(UNKNOWN_EMAIL, "irrelevant"))

            med_unknown = statistics.median(unknown_times)
            med_wrong = statistics.median(wrong_pw_times)

            # The oracle direction: a known-email-wrong-password attempt costs
            # one scrypt, so if the unknown-email path returns WITHOUT that
            # work it is dramatically FASTER — known/unknown blows past 3x.
            # The invariant is "same scrypt cost"; a ratio cap of 3× is
            # generous. (The inverse ratio would pass even with the oracle
            # present, which is the trap the original test fell into.)
            ratio = med_wrong / med_unknown if med_unknown > 0 else float("inf")
            self.assertLess(
                ratio, 3.0,
                f"unknown-user signin is {ratio:.2f}× faster than wrong-password "
                f"({med_unknown:.2f}ms vs {med_wrong:.2f}ms) — "
                f"possible user-enumeration timing oracle via HTTP",
            )
        finally:
            httpd.shutdown()
            httpd.server_close()
            if service is not None:
                try:
                    service.backend.close()
                except Exception:
                    pass
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _http_post(self, base: str, path: str, body: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        # Retry transient connection-level failures (socket reset, refused) a
        # couple of times so a one-off blip does not error a timing test.
        last_error: Exception | None = None
        for _attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
            except urllib.error.HTTPError as exc:
                # HTTPError is a SUBCLASS of URLError; it must be caught first
                # so an expected 401 is returned, never retried as transport.
                payload = {}
                try:
                    raw = exc.read()
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    pass
                finally:
                    exc.close()  # release the unread response body / socket
                return exc.code, payload
            except urllib.error.URLError as exc:
                last_error = exc
                continue
        raise last_error  # type: ignore[misc]  # three retries consumed

    # ------------------------------------------------------------------
    # 9.2 Token refusals
    # ------------------------------------------------------------------

    def test_03_expired_verification_token_refused(self):
        """#4: expired verification token raises AuthError("invalid_token")."""
        make_tenant(self.backend, "tenant_a", "Org A")
        _, token = self.accounts.signup(self.backend, "tenant_a", KNOWN_EMAIL, KNOWN_PASSWORD)

        # Force the token to expire by setting verification_expires_at in the past.
        with self.backend.transaction() as tx:
            tx.execute(
                "UPDATE cloud_identity_accounts SET verification_expires_at = 0.0 "
                "WHERE email = ? AND tenant_id = ?",
                (KNOWN_EMAIL, "tenant_a"),
            )
            tx.commit()

        with self.assertRaises(Exception) as exc:
            self.accounts.verify_email(self.backend, token)
        self.assertEqual(exc.exception.args[0], "invalid_token")

    def test_04_reused_verification_token_refused(self):
        """#5: a verification token consumed once cannot be reused."""
        make_tenant(self.backend, "tenant_a", "Org A")
        _, token = self.accounts.signup(self.backend, "tenant_a", KNOWN_EMAIL, KNOWN_PASSWORD)

        # First consume succeeds.
        self.accounts.verify_email(self.backend, token)

        # Second consume must fail.
        with self.assertRaises(Exception) as exc:
            self.accounts.verify_email(self.backend, token)
        self.assertEqual(exc.exception.args[0], "invalid_token")

    def test_05_tampered_verification_token_refused(self):
        """#6: a mutated token raises AuthError("invalid_token")."""
        make_tenant(self.backend, "tenant_a", "Org A")
        _, token = self.accounts.signup(self.backend, "tenant_a", KNOWN_EMAIL, KNOWN_PASSWORD)

        tampered = token[:-4] + "XXXX"

        with self.assertRaises(Exception) as exc:
            self.accounts.verify_email(self.backend, tampered)
        self.assertEqual(exc.exception.args[0], "invalid_token")

    def test_06_reset_token_cannot_be_replayed(self):
        """#8: a reset token consumed once cannot be replayed."""
        make_tenant(self.backend, "tenant_a", "Org A")
        self.accounts.signup(self.backend, "tenant_a", KNOWN_PASSWORD, KNOWN_PASSWORD)
        self.accounts.request_password_reset(self.backend, "tenant_a", KNOWN_PASSWORD)

        # Pull the raw reset token from the outbox (Stage 1: LocalOutboxMailer).
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? ORDER BY created_at DESC LIMIT 1",
                (KNOWN_PASSWORD.casefold(),),
            ).fetchone()
        # The reset URL/body contains the token — extract it.
        import re
        m = re.search(r"(frt_[A-Za-z0-9_-]+)", row["body"])
        raw_reset_token = m.group(1)

        # First reset succeeds.
        self.accounts.reset_password(self.backend, raw_reset_token, "New-Password-!99")

        # Second reset with same token must fail.
        with self.assertRaises(Exception) as exc:
            self.accounts.reset_password(self.backend, raw_reset_token, "Another-Password-!11")
        self.assertEqual(exc.exception.args[0], "invalid_token")

    def test_07_reset_kills_existing_sessions(self):
        """#9: reset_password revokes ALL sessions for the account."""
        make_tenant(self.backend, "tenant_a", "Org A")
        account_id, _ = self.accounts.signup(
            self.backend, "tenant_a", KNOWN_EMAIL, KNOWN_PASSWORD
        )
        # Create an owner membership so we can issue a session.
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', datetime('now'))",
                ("tenant_a", account_id),
            )
            tx.commit()

        sid, raw_token = self.sessions.create(self.backend, "tenant_a", account_id, "owner")

        # Request + perform reset.
        self.accounts.request_password_reset(self.backend, "tenant_a", KNOWN_EMAIL)
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? ORDER BY created_at DESC LIMIT 1",
                (KNOWN_EMAIL,),
            ).fetchone()
        import re
        m = re.search(r"(frt_[A-Za-z0-9_-]+)", row["body"])
        raw_reset_token = m.group(1)
        self.accounts.reset_password(self.backend, raw_reset_token, "New-Password-!99")

        # Old session must now be invalid.
        with self.assertRaises(Exception) as exc:
            self.sessions.validate(self.backend, raw_token)
        self.assertEqual(exc.exception.args[0], "invalid_session")

    # ------------------------------------------------------------------
    # 9.3 Session refusals
    # ------------------------------------------------------------------

    def test_08_revoked_session_cannot_act(self):
        """#10: a revoked session token fails validation."""
        make_tenant(self.backend, "tenant_a", "Org A")
        account_id, _ = self.accounts.signup(
            self.backend, "tenant_a", KNOWN_EMAIL, KNOWN_PASSWORD
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', datetime('now'))",
                ("tenant_a", account_id),
            )
            tx.commit()

        sid, raw_token = self.sessions.create(self.backend, "tenant_a", account_id, "owner")
        self.sessions.revoke(self.backend, sid)

        with self.assertRaises(Exception) as exc:
            self.sessions.validate(self.backend, raw_token)
        self.assertEqual(exc.exception.args[0], "invalid_session")

    # ------------------------------------------------------------------
    # 9.4 Role enforcement refusals
    # ------------------------------------------------------------------

    def test_09_member_cannot_do_admin_actions(self):
        """#13: member.require_role("admin") raises RoleError("forbidden")."""
        make_tenant(self.backend, "tenant_a", "Org A")
        account_id, _ = self.accounts.signup(
            self.backend, "tenant_a", KNOWN_EMAIL, KNOWN_PASSWORD
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'member', datetime('now'))",
                ("tenant_a", account_id),
            )
            tx.commit()

        ctx = SessionContext(
            tenant_id="tenant_a",
            account_id=account_id,
            role="member",
            backend=self.backend,
        )

        with self.assertRaises(RoleError) as exc:
            ctx.require_role("admin")
        self.assertEqual(exc.exception.args[0], "forbidden")

    def test_10_admin_cannot_do_owner_only_actions(self):
        """#15: admin.require_role("owner") raises RoleError("forbidden")."""
        make_tenant(self.backend, "tenant_a", "Org A")
        account_id, _ = self.accounts.signup(
            self.backend, "tenant_a", KNOWN_EMAIL, KNOWN_PASSWORD
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'admin', datetime('now'))",
                ("tenant_a", account_id),
            )
            tx.commit()

        ctx = SessionContext(
            tenant_id="tenant_a",
            account_id=account_id,
            role="admin",
            backend=self.backend,
        )

        with self.assertRaises(RoleError) as exc:
            ctx.require_role("owner")
        self.assertEqual(exc.exception.args[0], "forbidden")

    # ------------------------------------------------------------------
    # 9.5 Invite refusals
    # ------------------------------------------------------------------

    def test_11_invite_cannot_be_redeemed_twice(self):
        """#19: accepting the same invite twice raises AuthError("invite_consumed")."""
        make_tenant(self.backend, "tenant_a", "Org A")
        owner_id, _ = self.accounts.signup(
            self.backend, "tenant_a", "owner@corp.com", KNOWN_PASSWORD
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', datetime('now'))",
                ("tenant_a", owner_id),
            )
            tx.commit()

        owner_ctx = SessionContext(
            tenant_id="tenant_a", account_id=owner_id, role="owner", backend=self.backend
        )
        _, invite_token = self.invites.create(owner_ctx, "newcomer@corp.com", "member")

        # First accept succeeds.
        self.invites.accept(self.backend, invite_token, "newcomer@corp.com", "Fresh-Pw-!01")

        # Second accept fails.
        with self.assertRaises(Exception) as exc:
            self.invites.accept(self.backend, invite_token, "newcomer@corp.com", "Fresh-Pw-!02")
        self.assertEqual(exc.exception.args[0], "invite_consumed")

    def test_12_invite_cannot_self_escalate(self):
        """#21: invite to 'member' yields 'member', never 'owner'."""
        make_tenant(self.backend, "tenant_a", "Org A")
        owner_id, _ = self.accounts.signup(
            self.backend, "tenant_a", "owner@corp.com", KNOWN_PASSWORD
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', datetime('now'))",
                ("tenant_a", owner_id),
            )
            tx.commit()

        owner_ctx = SessionContext(
            tenant_id="tenant_a", account_id=owner_id, role="owner", backend=self.backend
        )
        _, invite_token = self.invites.create(owner_ctx, "newcomer@corp.com", "member")

        new_account_id, _ = self.invites.accept(
            self.backend, invite_token, "newcomer@corp.com", "Fresh-Pw-!01"
        )

        # The membership role is EXACTLY the invite's role.
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT role FROM cloud_identity_members WHERE tenant_id = ? AND account_id = ?",
                ("tenant_a", new_account_id),
            ).fetchone()
        self.assertEqual(row["role"], "member")

    def test_13_invite_targets_specific_org(self):
        """#22: invite lands in the invite's tenant; org-A session cannot read org-B."""
        make_tenant(self.backend, "tenant_a", "Org A")
        make_tenant(self.backend, "tenant_b", "Org B")

        owner_a, _ = self.accounts.signup(
            self.backend, "tenant_a", "owner-a@corp.com", KNOWN_PASSWORD
        )
        owner_b, _ = self.accounts.signup(
            self.backend, "tenant_b", "owner-b@corp.com", KNOWN_PASSWORD
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', datetime('now'))",
                ("tenant_a", owner_a),
            )
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', datetime('now'))",
                ("tenant_b", owner_b),
            )
            tx.commit()

        owner_b_ctx = SessionContext(
            tenant_id="tenant_b", account_id=owner_b, role="owner", backend=self.backend
        )
        _, invite_token = self.invites.create(owner_b_ctx, "joiner@corp.com", "member")

        new_account_id, _ = self.invites.accept(
            self.backend, invite_token, "joiner@corp.com", "Fresh-Pw-!01"
        )

        # The membership is in tenant_b — verify directly.
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT tenant_id FROM cloud_identity_members WHERE account_id = ?",
                (new_account_id,),
            ).fetchone()
        self.assertEqual(row["tenant_id"], "tenant_b")

        # org-A owner lists members — must NOT see the joiner who landed in org-B.
        owner_a_ctx = SessionContext(
            tenant_id="tenant_a", account_id=owner_a, role="owner", backend=self.backend
        )
        members_a = self.orgs.list_members(owner_a_ctx)
        member_ids = {m["account_id"] for m in members_a}
        self.assertNotIn(new_account_id, member_ids)

    # ------------------------------------------------------------------
    # 9.6 Tenant isolation refusals
    # ------------------------------------------------------------------

    def test_14_cross_tenant_data_invisible(self):
        """#23/#24: org-A session sees only org-A members; org-B calls return empty."""
        make_tenant(self.backend, "tenant_a", "Org A")
        make_tenant(self.backend, "tenant_b", "Org B")

        owner_a, _ = self.accounts.signup(
            self.backend, "tenant_a", "owner-a@corp.com", KNOWN_PASSWORD
        )
        owner_b, _ = self.accounts.signup(
            self.backend, "tenant_b", "owner-b@corp.com", KNOWN_PASSWORD
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', datetime('now'))",
                ("tenant_a", owner_a),
            )
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', datetime('now'))",
                ("tenant_b", owner_b),
            )
            tx.commit()

        owner_a_ctx = SessionContext(
            tenant_id="tenant_a", account_id=owner_a, role="owner", backend=self.backend
        )
        owner_b_ctx = SessionContext(
            tenant_id="tenant_b", account_id=owner_b, role="owner", backend=self.backend
        )

        members_a = self.orgs.list_members(owner_a_ctx)
        members_b = self.orgs.list_members(owner_b_ctx)

        # Each org sees exactly one member — its own owner.
        self.assertEqual(len(members_a), 1)
        self.assertEqual(members_a[0]["account_id"], owner_a)
        self.assertEqual(len(members_b), 1)
        self.assertEqual(members_b[0]["account_id"], owner_b)

    # ------------------------------------------------------------------
    # 9.7 No-secrets-in-logs refusals
    # ------------------------------------------------------------------

    def test_15_no_raw_secret_in_error_messages(self):
        """#26: no raw password or raw token in any exception message."""
        make_tenant(self.backend, "tenant_a", "Org A")
        account_id, verify_token = self.accounts.signup(
            self.backend, "tenant_a", KNOWN_EMAIL, KNOWN_PASSWORD
        )

        # Collect raw secrets that must never appear in error strings.
        raw_secrets = [KNOWN_PASSWORD, WRONG_PASSWORD, verify_token]

        # Trigger a wrong-password error.
        with self.assertRaises(Exception) as exc:
            self.accounts.authenticate(self.backend, "tenant_a", KNOWN_EMAIL, WRONG_PASSWORD)
        msg = str(exc.exception)
        for secret in raw_secrets:
            self.assertNotIn(secret, msg,
                             f"raw secret leaked in error message: {msg!r}")

        # Trigger a tampered-token error.
        tampered = verify_token[:-4] + "XXXX"
        with self.assertRaises(Exception) as exc:
            self.accounts.verify_email(self.backend, tampered)
        msg = str(exc.exception)
        for secret in raw_secrets:
            self.assertNotIn(secret, msg,
                             f"raw secret leaked in tampered-token error: {msg!r}")

        # Trigger an invalid-session error.
        with self.assertRaises(Exception) as exc:
            self.sessions.validate(self.backend, "fss_doesnotexist_0000000000000000000000")
        msg = str(exc.exception)
        for secret in raw_secrets:
            self.assertNotIn(secret, msg,
                             f"raw secret leaked in invalid-session error: {msg!r}")

    def test_16_no_raw_secret_in_serialised_responses(self):
        """#27: no raw password or raw token in any serialised response dict."""
        make_tenant(self.backend, "tenant_a", "Org A")
        account_id, verify_token = self.accounts.signup(
            self.backend, "tenant_a", KNOWN_EMAIL, KNOWN_PASSWORD
        )
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', datetime('now'))",
                ("tenant_a", account_id),
            )
            tx.commit()

        _, session_token = self.sessions.create(self.backend, "tenant_a", account_id, "owner")

        # The raw session token must NEVER appear in any returned dict.
        ctx = self.sessions.validate(self.backend, session_token)
        # SessionContext is a dataclass — inspect its repr and fields.
        ctx_repr = repr(ctx)
        self.assertNotIn(session_token, ctx_repr)
        self.assertNotIn(KNOWN_PASSWORD, ctx_repr)

        # Account dict returned by a getter (if any) must not carry raw secrets.
        account = self.accounts.get_by_id(self.backend, account_id)
        if account is not None:
            serialised = str(account)
            self.assertNotIn(KNOWN_PASSWORD, serialised)
            self.assertNotIn(verify_token, serialised)
            self.assertNotIn(session_token, serialised)

    def test_17_grep_proof_no_raw_secrets_in_source(self):
        """#28: grep src/weft_cloud/identity/ for 'password'/'token'.

        Any hit must be a field name / param / comment — never a raw secret
        value. We verify by asserting that the actual test passwords and tokens
        we use never appear as literals in the identity source files.
        """
        import glob as _glob

        identity_dir = Path(__file__).resolve().parent.parent / "src" / "weft_cloud" / "identity"
        if not identity_dir.exists():
            self.skipTest("identity module not yet created (RED phase)")

        raw_secrets = [KNOWN_PASSWORD, WRONG_PASSWORD]
        hits = []
        for py_file in _glob.glob(str(identity_dir / "*.py")):
            text = Path(py_file).read_text(encoding="utf-8")
            for secret in raw_secrets:
                if secret in text:
                    hits.append((py_file, secret))

        self.assertEqual(hits, [],
                         f"raw secrets found in identity source: "
                         f"{[(p, s[:8]+'...') for p, s in hits]}")


if __name__ == "__main__":
    unittest.main()
