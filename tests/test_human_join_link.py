"""MPAI-64 regression tests:
1. Unauthenticated visitor to /j/<link_token> gets room details + sign-in / sign-up paths with return URL.
2. Authenticated non-member visitor to /j/<link_token> gets a 'Join this room' action that joins the room.
3. Authenticated existing member visitor to /j/<link_token> sees 'already a member' state and dashboard link.
4. POST /j/<link_token> executes the join and redirects to the room/app.
5. Machine JSON descriptor (Accept: application/json) remains unchanged.
"""

from __future__ import annotations

import http.client
import json
import time
import tempfile
import threading
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.storage import SqliteWalBackend
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.identity.accounts import signup
from weft_cloud.service import WeftCloudService, _CloudHTTPHandler


class TestHumanJoinLinkMPAI64(unittest.TestCase):
    """Regression test suite for human join link affordance on /j/<token> (MPAI-64)."""

    def setUp(self):
        import http.server

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "cloud.db")
        self.backend = SqliteWalBackend(self.db_path)
        self.backend.initialize()
        ensure_schema(self.backend)

        # Service server
        self.service = WeftCloudService(
            self.backend,
            origin="http://127.0.0.1:0",
        )
        _CloudHTTPHandler.service = self.service
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.service.origin = f"http://{self.host}:{self.port}"

        # Create an owner account and a room
        self.owner_email = f"owner_{time.time_ns()}@example.com"
        self.owner_acct, _ = signup(
            self.backend, "tenant_owner", self.owner_email, "Password123!"
        )
        _, self.owner_session = self.service.sessions.create(
            self.backend, "tenant_owner", self.owner_acct, "owner"
        )
        self.room = self.service.rooms.create_room(
            "tenant_owner",
            self.owner_acct,
            name="Alpha Collab Room",
            cap=6,
            actor_token=self.owner_session,
        )
        self.link_token = self.room["link_token"]
        self.room_id = self.room["room_id"]

        # Create a guest account
        self.guest_email = f"guest_{time.time_ns()}@example.com"
        self.guest_acct, _ = signup(
            self.backend, "tenant_guest", self.guest_email, "Password123!"
        )
        _, self.guest_session = self.service.sessions.create(
            self.backend, "tenant_guest", self.guest_acct, "owner"
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.backend.close()
        self._tmp.cleanup()

    def _get(self, path: str, cookie: str | None = None, accept: str = "text/html"):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {"Accept": accept}
        if cookie:
            headers["Cookie"] = f"fss_session={cookie}"
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp.status, body, resp.headers

    def _post(self, path: str, cookie: str | None = None, body_data: str = ""):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = f"fss_session={cookie}"
        conn.request("POST", path, body=body_data, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        headers_dict = dict(resp.getheaders())
        conn.close()
        return resp.status, body, headers_dict

    def test_unauthenticated_visitor_sees_login_and_signup_paths(self):
        """GET /j/<link_token> without session shows sign-in/sign-up call to action."""
        status, body, _ = self._get(f"/j/{self.link_token}")
        self.assertEqual(status, 200)
        self.assertIn("Alpha Collab Room", body)
        self.assertIn('href="/login', body)
        self.assertIn('href="/signup', body)
        # API documentation is still present
        self.assertIn("Connect an agent", body)

    def test_authenticated_non_member_sees_join_button_and_can_join(self):
        """GET /j/<link_token> with active guest session shows 'Join this room' action, and POST joins."""
        # 1. GET page as guest
        status, body, _ = self._get(f"/j/{self.link_token}", cookie=self.guest_session)
        self.assertEqual(status, 200)
        self.assertIn("Join this room", body)
        self.assertIn(f'action="/j/{self.link_token}"', body)

        # 2. POST to join
        post_status, _, headers = self._post(f"/j/{self.link_token}", cookie=self.guest_session)
        self.assertEqual(post_status, 303)
        self.assertIn("Location", headers)

        # 3. Verify guest is now an active member of the room
        with self.backend.transaction() as tx:
            member = tx.execute(
                "SELECT * FROM cloud_room_members WHERE room_id = ? AND agent_id = ? AND status = 'active'",
                (self.room_id, self.guest_acct),
            ).fetchone()
            self.assertIsNotNone(member)

        # 4. GET page again as guest -> now shows already a member
        status_after, body_after, _ = self._get(f"/j/{self.link_token}", cookie=self.guest_session)
        self.assertEqual(status_after, 200)
        self.assertIn("already a member", body_after.lower())

    def test_unauthenticated_post_join_redirects_to_login(self):
        """POST /j/<link_token> without session redirects to login with return path."""
        status, _, headers = self._post(f"/j/{self.link_token}")
        self.assertEqual(status, 303)
        self.assertTrue(headers.get("Location", "").startswith("/login"))

    def test_machine_json_descriptor_remains_intact(self):
        """GET /j/<link_token> with Accept: application/json returns the machine join descriptor."""
        status, body, _ = self._get(f"/j/{self.link_token}", accept="application/json")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["service"], "weft")
        self.assertEqual(data["room_id"], self.room_id)


if __name__ == "__main__":
    unittest.main()
