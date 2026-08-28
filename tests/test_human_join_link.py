"""MPAI-64 & MPAI-66 regression tests:
1. Unauthenticated visitor to /j/<link_token> gets room details + sign-in / sign-up paths with return URL.
2. Authenticated non-member visitor to /j/<link_token> gets a 'Join this room' action that joins the room.
3. Authenticated existing member visitor to /j/<link_token> sees 'already a member' state and dashboard link.
4. POST /j/<link_token> executes the join and redirects to the room/app.
5. Machine JSON descriptor (Accept: application/json) remains unchanged.
6. Real POST /v1/auth/signup sets fss_session cookie and enables browser join flow (MPAI-66).
7. Real POST /v1/auth/signin sets fss_session cookie and enables browser join flow (MPAI-66).
8. Real POST /v1/auth/signout clears fss_session cookie with Max-Age=0 (MPAI-66).
"""

from __future__ import annotations

import http.client
import json
import time
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.storage import SqliteWalBackend
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.identity.accounts import signup
from weft_cloud.rooms import RoomError
from weft_cloud.service import WeftCloudService, _CloudHTTPHandler


class TestHumanJoinLinkMPAI64(unittest.TestCase):
    """Regression test suite for human join link affordance on /j/<token> (MPAI-64 & MPAI-66)."""

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
        headers_dict = dict(resp.getheaders())
        conn.close()
        return resp.status, body, headers_dict

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

    def _post_json(self, path: str, payload: dict, auth_token: str | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        conn.request("POST", path, body=json.dumps(payload), headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        headers_dict = dict(resp.getheaders())
        conn.close()
        return resp.status, body, headers_dict

    def _extract_cookie(self, headers_dict: dict, cookie_name: str = "fss_session") -> str | None:
        set_cookie = headers_dict.get("Set-Cookie") or headers_dict.get("set-cookie")
        if not set_cookie:
            return None
        for part in set_cookie.split(";"):
            part = part.strip()
            if part.startswith(f"{cookie_name}="):
                return part[len(f"{cookie_name}="):]
        return None

    def test_unauthenticated_visitor_sees_login_and_signup_paths(self):
        """GET /j/<link_token> without session shows sign-in/sign-up call to action."""
        status, body, _ = self._get(f"/j/{self.link_token}")
        self.assertEqual(status, 200)
        self.assertIn("Alpha Collab Room", body)
        self.assertIn('href="/login', body)
        self.assertIn('href="/signup', body)
        self.assertIn("Your invitation", body)
        self.assertIn("Keep this link private", body)
        self.assertIn("Technical setup", body)
        self.assertLess(
            body.index("Join Alpha Collab Room"),
            body.index("Your invitation"),
        )
        self.assertLess(
            body.index("Your invitation"),
            body.index("Technical setup"),
        )
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
        self.assertEqual(
            headers.get("Location"),
            f"/app/room?id={self.room_id}",
            "a newly joined visitor must land in the room they accepted",
        )

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

    def test_expired_join_link_renders_actionable_gone_page(self):
        """An expired link explains why it cannot be used and offers a way forward."""
        with self.backend.transaction() as tx:
            tx.execute(
                "UPDATE cloud_room_links SET expires_at = ? WHERE link_id = ?",
                (time.time() - 1, self.room["link_id"]),
            )
            tx.commit()

        status, body, headers = self._get(f"/j/{self.link_token}")
        self.assertEqual(status, 410)
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertIn("<h1>Join link expired</h1>", body)
        self.assertIn("Ask the room owner for a new link", body)
        self.assertIn('href="/login"', body)
        self.assertIn('href="/signup"', body)
        self.assertNotIn("Traceback", body)

    def test_closed_room_link_renders_actionable_gone_page(self):
        """A link for a closed room tells the visitor the room is closed."""
        with self.backend.transaction() as tx:
            tx.execute(
                "UPDATE cloud_rooms SET state = 'closed' WHERE room_id = ?",
                (self.room_id,),
            )
            tx.commit()

        status, body, _ = self._get(f"/j/{self.link_token}")
        self.assertEqual(status, 410)
        self.assertIn("<h1>Room is closed</h1>", body)
        self.assertIn("Ask the room owner to create a new room", body)
        self.assertIn('href="/"', body)

    def test_join_failure_does_not_redirect_into_a_dead_end(self):
        """A room closing between GET and POST renders the reason instead of redirecting anyway."""
        with mock.patch.object(
            self.service.rooms,
            "join_room",
            side_effect=RoomError("room_closed", "Room is closed", 409),
        ):
            status, body, headers = self._post(
                f"/j/{self.link_token}", cookie=self.guest_session,
            )
        self.assertEqual(status, 409)
        self.assertNotIn("Location", headers)
        self.assertIn("<h1>Room is closed</h1>", body)
        self.assertIn("Ask the room owner to create a new room", body)

    def test_join_route_server_error_renders_actionable_html(self):
        """A descriptor storage failure must not disconnect the browser or expose a traceback."""
        with mock.patch.object(
            self.service.rooms,
            "resolve_room_by_link_token",
            side_effect=RuntimeError("database internals"),
        ):
            status, body, headers = self._get(f"/j/{self.link_token}")
        self.assertEqual(status, 500)
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertIn("<h1>We couldn", body)
        self.assertIn("load this room link</h1>", body)
        self.assertIn("Try again", body)
        self.assertIn('href="/login"', body)
        self.assertIn('href="/signup"', body)
        self.assertNotIn("database internals", body)
        self.assertNotIn("Traceback", body)

    def test_real_browser_signup_sets_cookie_and_enables_join_button(self):
        """POST /v1/auth/signup issues Set-Cookie fss_session, and GET /j/<token> sees it (MPAI-66)."""
        new_email = f"browser_{time.time_ns()}@example.com"
        status, body, headers = self._post_json(
            "/v1/auth/signup",
            {"email": new_email, "password": "Password123!"},
        )
        self.assertEqual(status, 201)
        raw_cookie = self._extract_cookie(headers, "fss_session")
        self.assertIsNotNone(raw_cookie, "Set-Cookie must be present in signup response")
        self.assertTrue(raw_cookie.startswith("fss_"), f"Cookie must be an fss_ token, got {raw_cookie}")

        # Now browser presents this cookie to GET /j/<link_token>
        get_status, get_body, _ = self._get(f"/j/{self.link_token}", cookie=raw_cookie)
        self.assertEqual(get_status, 200)
        self.assertIn("Join this room", get_body)

        # POST /j/<link_token> to join room
        post_status, _, post_headers = self._post(
            f"/j/{self.link_token}", cookie=raw_cookie,
        )
        self.assertEqual(post_status, 303)
        self.assertEqual(
            post_headers.get("Location"),
            f"/app/room?id={self.room_id}",
            "signup followed by Join must open the invited room",
        )

    def test_real_browser_signin_sets_cookie_and_enables_join_button(self):
        """POST /v1/auth/signin issues Set-Cookie fss_session, and GET /j/<token> sees it (MPAI-66)."""
        status, body, headers = self._post_json(
            "/v1/auth/signin",
            {"email": self.guest_email, "password": "Password123!"},
        )
        self.assertEqual(status, 200)
        raw_cookie = self._extract_cookie(headers, "fss_session")
        self.assertIsNotNone(raw_cookie, "Set-Cookie must be present in signin response")
        self.assertTrue(raw_cookie.startswith("fss_"), f"Cookie must be an fss_ token, got {raw_cookie}")

        # GET /j/<link_token> with this cookie
        get_status, get_body, _ = self._get(f"/j/{self.link_token}", cookie=raw_cookie)
        self.assertEqual(get_status, 200)
        self.assertIn("Join this room", get_body)

    def test_signout_clears_cookie(self):
        """POST /v1/auth/signout issues Set-Cookie fss_session=; Max-Age=0 (MPAI-66)."""
        status, body, headers = self._post_json(
            "/v1/auth/signout",
            {},
            auth_token=self.guest_session,
        )
        self.assertEqual(status, 200)
        set_cookie = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
        self.assertIn("fss_session=", set_cookie)
        self.assertIn("Max-Age=0", set_cookie)


if __name__ == "__main__":
    unittest.main()
