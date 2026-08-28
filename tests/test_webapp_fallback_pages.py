"""MPAI-63 regression tests:
1. /invite/<token> with invalid/expired/consumed token returns styled fallback with clear h1, explanation, and sign-in / sign-up paths.
2. /j/rm_<token> returns 404 with semantic <h1> and <main> landmark.
"""

from __future__ import annotations

import http.client
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.storage import SqliteWalBackend
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.web.app import WeftWebApp
from weft_cloud.service import WeftCloudService, _CloudHTTPHandler

SITE_DIR = str(ROOT / "site")


class TestFallbackPagesMPAI63(unittest.TestCase):
    """Regression test suite for fallback page styling and semantics (MPAI-63)."""

    def setUp(self):
        import http.server

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "cloud.db")
        self.backend = SqliteWalBackend(self.db_path)
        self.backend.initialize()
        ensure_schema(self.backend)

        # WebApp server
        self.app = WeftWebApp(
            self.backend,
            static_dir=SITE_DIR,
            state_dir=str(Path(self._tmp.name) / "state"),
        )
        self.app_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.app.handler)
        self.app_thread = threading.Thread(target=self.app_server.serve_forever, daemon=True)
        self.app_thread.start()
        self.app_host, self.app_port = self.app_server.server_address

        # CloudService server (for /j/ descriptor)
        self.service = WeftCloudService(
            self.backend,
            origin=f"http://127.0.0.1:0",
        )
        _CloudHTTPHandler.service = self.service
        self.svc_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.svc_thread = threading.Thread(target=self.svc_server.serve_forever, daemon=True)
        self.svc_thread.start()
        self.svc_host, self.svc_port = self.svc_server.server_address
        self.service.origin = f"http://{self.svc_host}:{self.svc_port}"

    def tearDown(self):
        self.app_server.shutdown()
        self.app_server.server_close()
        self.svc_server.shutdown()
        self.svc_server.server_close()
        self.backend.close()
        self._tmp.cleanup()

    def _get_app(self, path: str, cookie: str | None = None):
        conn = http.client.HTTPConnection(self.app_host, self.app_port, timeout=10)
        headers = {"Cookie": f"fss_session={cookie}"} if cookie else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp.status, body

    def _get_svc(self, path: str, accept: str = "text/html"):
        conn = http.client.HTTPConnection(self.svc_host, self.svc_port, timeout=10)
        conn.request("GET", path, headers={"Accept": accept})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp.status, body

    def test_invalid_invite_get_returns_styled_fallback_with_heading_and_exit_paths(self):
        """GET /invite/<invalid_token> renders styled fallback with h1, explanation, and sign-in/sign-up links."""
        status, body = self._get_app("/invite/fiv_invalid_cutover_test_token")
        self.assertEqual(status, 200)
        # Clear invalid link heading
        self.assertIn("<h1>Invalid or expired invite", body)
        # Plain-language explanation
        self.assertIn("This invite link is invalid, has expired, or has already been used", body)
        # Exit paths to sign in and sign up
        self.assertIn('href="/login"', body)
        self.assertIn('href="/signup"', body)
        # Shared styling present in head (not unstyled Times New Roman)
        self.assertIn("<style>", body)
        self.assertIn("font-family", body)
        # Semantic main landmark present
        self.assertIn("<main", body)

    def test_valid_invite_get_renders_styled_form(self):
        """GET /invite/<valid_token> renders styled accept invite form with csrf, email, password, and DASH_CSS."""
        from weft_cloud.identity.accounts import signup
        from weft_cloud.identity.context import SessionContext
        from weft_cloud.identity import invites
        from weft_cloud.storage import utc_now_iso
        acct_id, verify_token = signup(self.backend, "tenant_test", "admin@example.com", "Password123!")
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', ?)",
                ("tenant_test", acct_id, utc_now_iso()),
            )
            tx.commit()
        ctx = SessionContext(
            tenant_id="tenant_test",
            account_id=acct_id,
            role="owner",
            backend=self.backend,
        )
        invite_id, raw_token = invites.create(ctx, "invitee@example.com", "member")

        status, body = self._get_app(f"/invite/{raw_token}")
        self.assertEqual(status, 200)
        self.assertIn("<h1>Accept invite</h1>", body)
        self.assertIn('name="email"', body)
        self.assertIn('name="password"', body)
        self.assertIn("<style>", body)
        self.assertIn("<main", body)
        self.assertIn("</main>", body)

    def test_invalid_join_route_returns_404_with_h1_and_main_landmark(self):
        """GET /j/rm_<invalid_token> returns 404 with h1 and <main> landmark."""
        status, body = self._get_svc("/j/rm_invalid_cutover", accept="text/html")
        self.assertEqual(status, 404)
        self.assertIn("<main", body)
        self.assertIn("</main>", body)
        self.assertIn("<h1>", body)
        self.assertIn("</h1>", body)
        self.assertIn("This link does not open a room.", body)

    def test_valid_join_route_returns_200_with_h1_and_main_landmark(self):
        """GET /j/<valid_token> returns 200 with h1 and <main> landmark."""
        email = f"owner{time.time_ns()}@example.com"
        from weft_cloud.identity.accounts import signup
        acct_id, session_token = signup(self.backend, "tenant_test", email, "Password123!")
        room = self.service.rooms.create_room(
            "tenant_test",
            acct_id,
            name="demo-room",
            cap=5,
            actor_token=session_token,
        )
        link_token = room["link_token"]

        status, body = self._get_svc(f"/j/{link_token}", accept="text/html")
        self.assertEqual(status, 200)
        self.assertIn("<main", body)
        self.assertIn("</main>", body)
        self.assertIn("<h1>", body)
        self.assertIn("</h1>", body)
        self.assertIn("Connect an agent", body)

    def test_invite_storage_error_renders_actionable_html(self):
        """A database failure is not misreported as an invalid invite."""
        with mock.patch.object(
            self.backend,
            "transaction",
            side_effect=RuntimeError("database internals"),
        ):
            status, body = self._get_app("/invite/fiv_server_error_test_token")
        self.assertEqual(status, 500)
        self.assertIn("<h1>Something went wrong</h1>", body)
        self.assertIn("Try again", body)
        self.assertIn('href="/login"', body)
        self.assertIn('href="/signup"', body)
        self.assertNotIn("Invalid or expired invite", body)
        self.assertNotIn("database internals", body)
        self.assertNotIn("Traceback", body)

    def test_legacy_storage_error_renders_actionable_html(self):
        """A signed-in dashboard failure gets a useful HTML recovery page."""
        from weft_cloud.identity.accounts import signup

        email = f"legacy_error_{time.time_ns()}@example.com"
        account_id, _ = signup(
            self.backend, "tenant_legacy_error", email, "Password123!",
        )
        _, session_token = self.app.sessions.create(
            self.backend, "tenant_legacy_error", account_id, "owner",
        )
        with mock.patch.object(
            self.app,
            "_list_rooms_for_account",
            side_effect=RuntimeError("dashboard internals"),
        ):
            status, body = self._get_app("/legacy", cookie=session_token)
        self.assertEqual(status, 500)
        self.assertIn("<h1>Something went wrong</h1>", body)
        self.assertIn("Try again", body)
        self.assertIn('href="/legacy"', body)
        self.assertIn('href="/login"', body)
        self.assertNotIn("dashboard internals", body)
        self.assertNotIn("Traceback", body)


if __name__ == "__main__":
    unittest.main()
