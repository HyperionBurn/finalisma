"""Session persistence across service restarts — the deploy-logout regression.

Regression: after "deploy new code and restart the services", previously-issued
session tokens must STILL authenticate. The report that triggered this suite was
a production deploy after which every live session token returned 401 on
``POST /mcp tools/list`` while freshly-minted tokens worked immediately — a
silent logout of every human and every connected agent.

The properties locked here, each driven through the REAL HTTP surface:

  1. A session token minted before a restart still authenticates after the
     service is stopped and started again on the SAME database file.
  2. The same token authenticates after a database upgrade: an old-shaped DB
     with live sessions is brought up to the latest migrations (the deploy step)
     and the sessions still validate afterwards.
  3. Legitimate revocation still works after restart: signout revokes
     immediately, and password reset revokes every session for the account.

Security invariants that must NOT be weakened to make the above true: tokens
stay SHA-256 hashed at rest, expiry still applies, signout still revokes
immediately, and password reset still revokes all sessions.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 4, 9.3, 13.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

# Ensure the src package is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


def _get(base: str, path: str, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


def _mcp(base: str, method: str, token: str | None, request_id: int) -> tuple[int, dict | None]:
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(base + "/mcp", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = None
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


class _RunningServer:
    """A real HTTP service bound to ONE database file, on an ephemeral port."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.service = WeftCloudService(SqliteWalBackend(self.db_path))
        _CloudHTTPHandler.service = self.service
        self.thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        try:
            self.service.backend.close()
        except Exception:
            pass


class SessionRestartPersistenceTests(unittest.TestCase):
    """A session token minted before a restart still authenticates afterwards.

    Each test creates a file-backed DB, signs in over real HTTP to obtain a
    session token, stops the service, starts a fresh service on the SAME file,
    and re-validates the token over real HTTP — the exact path that broke in the
    production deploy report.
    """

    def _new_db(self) -> str:
        self.tmpdir = tempfile.mkdtemp(prefix="weft-session-restart-")
        return str(Path(self.tmpdir) / "state.db")

    def _signup_token(self, base: str, email: str) -> str:
        status, resp = _post(base, "/v1/auth/signup",
                             {"email": email, "password": "password-123"})
        self.assertEqual(status, 201, f"signup failed: {resp}")
        return resp["session_token"]

    def _signin_token(self, base: str, email: str) -> str:
        """Sign up (if needed) then sign in; return the signin-issued token."""
        status, _ = _post(base, "/v1/auth/signup",
                          {"email": email, "password": "password-123"})
        if status != 201 and status != 400:
            raise AssertionError(f"signup precondition failed: HTTP {status}")
        status, resp = _post(base, "/v1/auth/signin",
                             {"email": email, "password": "password-123"})
        self.assertEqual(status, 200, f"signin failed: {resp}")
        return resp["session_token"]

    def test_token_survives_service_restart_on_same_db(self) -> None:
        """The core property: restart must not log a session out.

        Sign in, stop the service, start it again on the SAME database file,
        and the SAME token must still authenticate against POST /mcp.
        """
        db_path = self._new_db()
        srv1 = _RunningServer(db_path)
        try:
            token = self._signin_token(srv1.base, "restart@example.com")
            # Sanity: valid before the restart.
            status, listing = _mcp(srv1.base, "tools/list", token, 1)
            self.assertEqual(status, 200, f"pre-restart tools/list failed: {listing}")
        finally:
            srv1.stop()

        # Restart: a fresh service on the same file is a plain service restart.
        srv2 = _RunningServer(db_path)
        try:
            status, listing = _mcp(srv2.base, "tools/list", token, 2)
            self.assertEqual(
                status, 200,
                f"SAME token rejected after restart (deploy-logout regression): "
                f"HTTP {status}: {listing}",
            )
            # /v1/me must agree.
            status, me = _get(srv2.base, "/v1/me", token=token)
            self.assertEqual(status, 200, f"/v1/me rejected after restart: {me}")
        finally:
            srv2.stop()

    def test_restart_does_not_revoke_or_expire_sessions(self) -> None:
        """The session row must survive the restart untouched: not revoked, not expired.

        Directly asserts the DB invariants after a restart, so a future change
        that revokes or deletes sessions on boot fails here with a precise cause.
        """
        import hashlib
        import time as _time

        db_path = self._new_db()
        srv1 = _RunningServer(db_path)
        try:
            token = self._signup_token(srv1.base, "row-inspect@example.com")
            status, me = _get(srv1.base, "/v1/me", token=token)
            self.assertEqual(status, 200)
        finally:
            srv1.stop()

        srv2 = _RunningServer(db_path)  # restart on the same file
        try:
            status, listing = _mcp(srv2.base, "tools/list", token, 3)
            self.assertEqual(status, 200, f"token invalid after restart: {listing}")
            # Inspect the row directly: not revoked, not expired, hashed.
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT token_hash, revoked_at, expires_at, role_snapshot "
                    "FROM cloud_identity_sessions"
                ).fetchone()
                self.assertIsNotNone(row, "session row must survive a restart")
                self.assertIsNone(row["revoked_at"], "restart must not revoke sessions")
                self.assertGreater(row["expires_at"], _time.time(),
                                   "restart must not expire a valid session")
                self.assertNotIn(token, dict(row).values(),
                                 "raw token must never be stored at rest")
                expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
                self.assertEqual(row["token_hash"], expected,
                                 "token must stay SHA-256 hashed at rest")
            finally:
                conn.close()
        finally:
            srv2.stop()

    def test_signout_still_revokes_after_restart(self) -> None:
        """Signout must still revoke immediately — even after a restart."""
        db_path = self._new_db()
        srv1 = _RunningServer(db_path)
        try:
            token = self._signin_token(srv1.base, "signout@example.com")
        finally:
            srv1.stop()

        srv2 = _RunningServer(db_path)
        try:
            status, resp = _post(srv2.base, "/v1/auth/signout", {}, token=token)
            self.assertEqual(status, 200, f"signout failed: {resp}")
            status, listing = _mcp(srv2.base, "tools/list", token, 4)
            self.assertEqual(status, 401,
                             "signout must revoke the token immediately")
        finally:
            srv2.stop()

    def test_password_reset_revokes_all_sessions_after_restart(self) -> None:
        """Password reset must still revoke EVERY session of the account."""
        db_path = self._new_db()
        srv1 = _RunningServer(db_path)
        try:
            token_a = self._signin_token(srv1.base, "reset@example.com")
            token_b = self._signin_token(srv1.base, "reset@example.com")
        finally:
            srv1.stop()

        srv2 = _RunningServer(db_path)
        try:
            # Request a reset and consume it through the real identity API.
            from weft_cloud.identity.accounts import (
                request_password_reset,
                reset_password,
            )
            with srv2.service.backend.transaction() as tx:
                row = tx.execute(
                    "SELECT tenant_id FROM cloud_identity_accounts WHERE email = ?",
                    ("reset@example.com",),
                ).fetchone()
                tenant_id = row["tenant_id"]
            request_password_reset(srv2.service.backend, tenant_id, "reset@example.com")
            # The raw reset token is written to the identity outbox by the
            # LocalOutboxMailer — the ONLY place the raw token exists.
            with srv2.service.backend.transaction() as tx:
                row = tx.execute(
                    "SELECT body FROM cloud_identity_outbox "
                    "WHERE to_email = ? ORDER BY created_at DESC LIMIT 1",
                    ("reset@example.com",),
                ).fetchone()
                raw_reset = row["body"].split("Reset your password: ")[-1].strip()
            reset_password(srv2.service.backend, raw_reset, "New-Password-!99")

            # Both sessions must now be revoked.
            for i, token in enumerate((token_a, token_b)):
                status, listing = _mcp(srv2.base, "tools/list", token, 10 + i)
                self.assertEqual(
                    status, 401,
                    f"password reset must revoke every session (token {i}): "
                    f"HTTP {status}: {listing}",
                )
        finally:
            srv2.stop()


class MigrationPreservesLiveSessionsTests(unittest.TestCase):
    """Applying the migrations to a DB with live sessions must not log them out.

    The deploy step is the migration application: an old-shaped DB (pre-cloud_008
    outbox, pre-cloud_009 message_kind, no cloud_010/011) with LIVE session rows
    is brought up to the latest schema. Every session must still validate.
    """

    _PRE_CLOUD_008_MIGRATION_IDS = (
        "cloud_001_init",
        "cloud_002_identity_accounts",
        "cloud_003_identity_sessions",
        "cloud_004_identity_members",
        "cloud_005_identity_invites",
        "cloud_006_identity_outbox",
        "cloud_007_room_tables",
    )

    def setUp(self) -> None:
        from weft_cloud.migrations import (
            _CLOUD_TABLES_SQL,
            _IDENTITY_ACCOUNTS_SQL,
            _IDENTITY_SESSIONS_SQL,
            _IDENTITY_MEMBERS_SQL,
            _IDENTITY_INVITES_SQL,
            _IDENTITY_OUTBOX_SQL,
            _ROOM_TABLES_SQL,
        )
        self.tmp = tempfile.TemporaryDirectory(prefix="weft-mig-sessions-")
        self.cloud_path = str(Path(self.tmp.name) / "cloud.db")
        self.backend = SqliteWalBackend(self.cloud_path)
        self.backend.initialize()
        # Old production shape: identity outbox WITHOUT delivery columns and
        # event log WITHOUT message_kind — the pre-cloud_008 schema.
        event_log_pre_kind = _ROOM_TABLES_SQL.replace(
            "    kind TEXT NOT NULL,\n    message_kind TEXT,\n",
            "    kind TEXT NOT NULL,\n",
            1,
        )
        assert "message_kind" not in event_log_pre_kind
        schema = (
            _CLOUD_TABLES_SQL
            + _IDENTITY_ACCOUNTS_SQL
            + _IDENTITY_SESSIONS_SQL
            + _IDENTITY_MEMBERS_SQL
            + _IDENTITY_INVITES_SQL
            + _IDENTITY_OUTBOX_SQL
            + event_log_pre_kind
        )
        from weft_cloud.storage import utc_now_iso
        now = utc_now_iso()
        with self.backend._transaction() as conn:
            conn.executescript(schema)
            for migration_id in self._PRE_CLOUD_008_MIGRATION_IDS:
                conn.execute(
                    "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                    (migration_id, now),
                )
            conn.execute(
                "INSERT INTO cloud_tenants(tenant_id, name, plan_id, created_at) VALUES (?, ?, ?, ?)",
                ("tenant_a", "Org A", "free", now),
            )
            conn.execute(
                "INSERT INTO cloud_identity_accounts("
                " account_id, tenant_id, email, salt, password_hash, created_at)"
                " VALUES ('acct_001', 'tenant_a', 'live@example.com', ?, ?, ?)",
                (b"\x00" * 16, b"\x00" * 32, now),
            )
            conn.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at)"
                " VALUES ('tenant_a', 'acct_001', 'owner', ?)",
                (now,),
            )

    def tearDown(self) -> None:
        try:
            self.backend.close()
        finally:
            import gc
            gc.collect()
            self.tmp.cleanup()

    def _seed_live_sessions(self) -> list[str]:
        """Create live session rows the way the OLD code did (raw token -> sha256)."""
        import hashlib
        import time as _time
        from weft_cloud.storage import utc_now_iso
        now = utc_now_iso()
        expire = _time.time() + 24 * 3600
        tokens = []
        with self.backend._transaction() as conn:
            for i in range(3):
                raw = f"fss_live_{i}_{hashlib.sha256(str(i).encode()).hexdigest()}"
                tokens.append(raw)
                conn.execute(
                    "INSERT INTO cloud_identity_sessions("
                    " session_id, tenant_id, account_id, token_hash, created_at,"
                    " expires_at, role_snapshot)"
                    " VALUES (?, 'tenant_a', 'acct_001', ?, ?, ?, 'owner')",
                    (f"ses_live_{i}", hashlib.sha256(raw.encode()).hexdigest(), now, expire),
                )
        return tokens

    def _tokens_validate(self, tokens: list[str]) -> None:
        for token in tokens:
            from weft_cloud.identity.sessions import validate
            ctx = validate(self.backend, token)
            self.assertEqual(ctx.account_id, "acct_001",
                             f"migration must not invalidate live session")

    def test_migrations_preserve_live_sessions(self) -> None:
        """The deploy migration step must leave live sessions valid."""
        from weft_cloud.identity.schema import ensure_schema

        tokens = self._seed_live_sessions()
        self._tokens_validate(tokens)  # valid in the old schema first

        # The deploy step: bring the old-shaped DB up to the latest migrations.
        ensure_schema(self.backend)

        # All live sessions still validate.
        self._tokens_validate(tokens)

        # And the schema_migrations ledger is complete (all 13 cloud migrations).
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            n = conn.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(n, 13, "all cloud migrations must be recorded after upgrade")

    def test_migrations_then_restart_preserves_live_sessions(self) -> None:
        """Migration + restart together (the exact deploy sequence)."""
        from weft_cloud.identity.schema import ensure_schema
        from weft_cloud.identity.sessions import validate

        tokens = self._seed_live_sessions()
        # Deploy step 1: upgrade the DB.
        ensure_schema(self.backend)
        self.backend.close()

        # Deploy step 2: restart the service on the upgraded DB and validate
        # over real HTTP using the SAME tokens.
        srv = _RunningServer(self.cloud_path)
        try:
            for token in tokens:
                status, listing = _mcp(srv.base, "tools/list", token, 1)
                self.assertEqual(
                    status, 200,
                    f"live session invalid after migration+restart: HTTP {status}: {listing}",
                )
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
