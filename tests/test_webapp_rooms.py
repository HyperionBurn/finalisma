"""Wave H — web room dashboard + connect-an-agent routes integration contract (RED).

The weft_cloud.web package does not exist yet — this file must fail at
import with ModuleNotFoundError.

Drives real HTTP against an in-process ThreadingHTTPServer. Contract per
WEBAPP_DESIGN.md §3.3, §6, §7, §9.3, §9.6, §10:

- WeftWebApp(backend, static_dir=..., state_dir=...) exposes .handler
  (a BaseHTTPRequestHandler subclass).
- The app creates rooms by instantiating weft_mcp.room.RoomStore on a
  per-tenant coordinator DB under state_dir (the app owns this).
- Owner (admin+) creates rooms; members view; cross-tenant is 404 (never 403);
  CSRF gates every state-changing POST; raw link token only on the connect page.
"""

from __future__ import annotations

import http.client
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.storage import SqliteWalBackend
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.web.app import WeftWebApp  # RED: package absent

SITE_DIR = str(ROOT / "site")


class WebAppDriver:
    """In-process HTTP driver for WeftWebApp (WEBAPP_DESIGN.md §10.1).

    Contract: WeftWebApp(backend, static_dir=..., state_dir=...)
    exposes .handler and is driven on ("127.0.0.1", 0) with finally teardown.
    """

    def __init__(self):
        import http.server

        self._tmp = tempfile.TemporaryDirectory()
        self.backend = SqliteWalBackend(str(Path(self._tmp.name) / "cloud.db"))
        self.backend.initialize()
        ensure_schema(self.backend)
        self.app = WeftWebApp(
            self.backend,
            static_dir=SITE_DIR,
            state_dir=str(Path(self._tmp.name) / "state"),
        )
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.app.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.cookies: dict[str, str] = {}

    def _headers(self):
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return {"Cookie": cookie} if cookie else {}

    def _collect_cookies(self, resp):
        for hdr, val in resp.getheaders():
            if hdr.lower() == "set-cookie":
                kv = val.split(";")[0]
                k, v = kv.split("=", 1)
                k = k.strip()
                v = v.strip()
                if "Max-Age=0" in val or v == "":
                    self.cookies.pop(k, None)
                else:
                    self.cookies[k] = v

    def get(self, path):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        conn.request("GET", path, headers=self._headers())
        resp = conn.getresponse()
        self._collect_cookies(resp)
        body = resp.read().decode("utf-8", errors="replace")
        headers = dict(resp.getheaders())
        conn.close()
        return resp.status, body, headers

    def post(self, path, form: dict):
        body = urlencode(form)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        conn.request(
            "POST",
            path,
            body=body,
            headers={**self._headers(), "Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = conn.getresponse()
        self._collect_cookies(resp)
        resp_body = resp.read().decode("utf-8", errors="replace")
        headers = dict(resp.getheaders())
        conn.close()
        return resp.status, resp_body, headers

    def extract_csrf(self, html):
        m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
        return m.group(1) if m else None

    def csrf(self, path="/"):
        """Fetch a page with the current session and return its CSRF token."""
        _, body, _ = self.get(path)
        token = self.extract_csrf(body)
        assert token is not None, f"no _csrf token found on {path}"
        return token

    def last_outbox_body(self, to_email):
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (to_email,),
            ).fetchone()
        return row["body"] if row else None

    def login(self, email, password):
        """Sign up + verify + login as a fresh user.

        Returns (status_of_login, cookie_set). The signup/verify POSTs include
        a _csrf key which the public pre-auth routes ignore.
        """
        self.post("/signup", {"email": email, "password": password, "_csrf": "x"})
        body = self.last_outbox_body(email)
        token = re.search(r"(fvt_[A-Za-z0-9_-]+)", body).group(1)
        self.post("/verify", {"token": token, "_csrf": "x"})
        status, _, _ = self.post("/login", {"email": email, "password": password})
        return status, "fss_session" in self.cookies

    def create_room(self, name="My Room", cap=8):
        """Owner logs in, POSTs /rooms, follows to the detail page; returns room_id."""
        html = self.get("/")[1]
        csrf = self.extract_csrf(html)
        status, _, headers = self.post("/rooms", {"name": name, "cap": str(cap), "_csrf": csrf})
        assert status == 303, f"create room expected 303, got {status}"
        loc = headers["Location"]
        m = re.search(r"/room/([A-Za-z0-9_-]+)", loc)
        return m.group(1)

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        finally:
            self.backend.close()
            self._tmp.cleanup()


class TestCreateRoom(unittest.TestCase):
    """§3.3 + §6.1: owner (admin+) can create a room → 303 to /room/{room_id}."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"owner{time.time_ns()}@example.com"
        self.password = "owner-password-ok"
        self.driver.login(self.email, self.password)

    def tearDown(self):
        self.driver.close()

    def test_create_room_returns_303_with_room_id(self):
        html = self.driver.get("/")[1]
        csrf = self.driver.extract_csrf(html)
        status, _, headers = self.driver.post(
            "/rooms", {"name": "War Room", "cap": "8", "_csrf": csrf}
        )
        self.assertEqual(status, 303)
        loc = headers["Location"]
        self.assertTrue(loc.startswith("/room/"))
        room_id = loc.split("/room/", 1)[1]
        self.assertTrue(
            re.fullmatch(r"room_[0-9a-f]+", room_id),
            f"room_id must match room_<hex>, got {room_id!r}",
        )


class TestListRooms(unittest.TestCase):
    """§3.3 + §6: GET /rooms lists the created room with a link to its detail page."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"owner{time.time_ns()}@example.com"
        self.password = "owner-password-ok"
        self.driver.login(self.email, self.password)
        self.room_id = self.driver.create_room(name="War Room")

    def tearDown(self):
        self.driver.close()

    def test_list_rooms_shows_room_name_and_link(self):
        status, body, _ = self.driver.get("/rooms")
        self.assertEqual(status, 200)
        self.assertIn("War Room", body)
        self.assertIn(f"/room/{self.room_id}", body)


class TestRoomDetail(unittest.TestCase):
    """§3.3 + §6.3/6.4: GET /room/{room_id} renders name, roster, event log."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"owner{time.time_ns()}@example.com"
        self.password = "owner-password-ok"
        self.driver.login(self.email, self.password)
        self.room_id = self.driver.create_room(name="War Room")

    def tearDown(self):
        self.driver.close()

    def test_room_detail_renders_name_roster_and_events(self):
        status, body, _ = self.driver.get(f"/room/{self.room_id}")
        self.assertEqual(status, 200)
        self.assertIn("War Room", body)
        # Owner is a member of the room.
        self.assertIn(self.email, body)
        # The ordered event log contains at least room.created and room.joined.
        self.assertIn("room.created", body)
        self.assertIn("room.joined", body)

    def test_room_detail_does_not_contain_raw_link_token(self):
        status, body, _ = self.driver.get(f"/room/{self.room_id}")
        self.assertEqual(status, 200)
        # The raw rm_ join link must NOT appear on the detail page.
        self.assertIsNone(re.search(r"rm_[A-Za-z0-9_-]+", body))


class TestEventPoll(unittest.TestCase):
    """§3.3 + §6.4: GET /room/{room_id}/events returns JSON; supports after_seq cursor."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"owner{time.time_ns()}@example.com"
        self.password = "owner-password-ok"
        self.driver.login(self.email, self.password)
        self.room_id = self.driver.create_room(name="War Room")

    def tearDown(self):
        self.driver.close()

    def test_events_initial_poll_returns_json_with_events_and_next_seq(self):
        status, body, _ = self.driver.get(f"/room/{self.room_id}/events?after_seq=0")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("events", data)
        self.assertIn("next_seq", data)
        self.assertIsInstance(data["events"], list)
        self.assertGreaterEqual(data["next_seq"], 1)
        kinds = {e["kind"] for e in data["events"]}
        self.assertIn("room.created", kinds)
        self.assertIn("room.joined", kinds)
        for e in data["events"]:
            self.assertIn("seq", e)
            self.assertIn("kind", e)
            self.assertIn("origin", e)

    def test_events_poll_past_end_returns_empty_with_next_seq_equal_cursor_head(self):
        # First poll to learn the cursor head.
        _, body, _ = self.driver.get(f"/room/{self.room_id}/events?after_seq=0")
        data = json.loads(body)
        cursor_head = data["next_seq"]
        # Poll with a cursor far past the end.
        status, body, _ = self.driver.get(
            f"/room/{self.room_id}/events?after_seq={cursor_head + 1000}"
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["events"], [])
        self.assertEqual(data["next_seq"], cursor_head)


class TestAuditLog(unittest.TestCase):
    """§3.3 + §6.5: GET /room/{room_id}/audit renders the audit log."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"owner{time.time_ns()}@example.com"
        self.password = "owner-password-ok"
        self.driver.login(self.email, self.password)
        self.room_id = self.driver.create_room(name="War Room")

    def tearDown(self):
        self.driver.close()

    def test_audit_page_renders_room_created_action(self):
        status, body, _ = self.driver.get(f"/room/{self.room_id}/audit")
        self.assertEqual(status, 200)
        self.assertIn("room.created", body)


class TestConnectPage(unittest.TestCase):
    """§3.3 + §7: GET /room/{room_id}/connect renders the four tiers + raw link token."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"owner{time.time_ns()}@example.com"
        self.password = "owner-password-ok"
        self.driver.login(self.email, self.password)
        self.room_id = self.driver.create_room(name="War Room")

    def tearDown(self):
        self.driver.close()

    def test_connect_page_renders_raw_link_token_and_four_tiers(self):
        status, body, _ = self.driver.get(f"/room/{self.room_id}/connect")
        self.assertEqual(status, 200)
        # The raw link token IS rendered on the connect page (copy-link UX).
        self.assertIsNotNone(
            re.search(r"rm_[A-Za-z0-9_-]+", body),
            "raw rm_ link token must appear on the connect page",
        )
        # Four tier sections.
        self.assertIn("mcpServers", body)        # Tier 1 — MCP stdio
        self.assertIn("Streamable HTTP", body)   # Tier 2 — MCP Streamable HTTP
        self.assertIn("bridge", body)            # Tier 3 — bridge/webhook
        self.assertIn("WeftClient", body)   # Tier 4 — SDK


class TestCloseRoom(unittest.TestCase):
    """§3.3 + §6.6: owner can close; after close the detail page renders a closed state."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"owner{time.time_ns()}@example.com"
        self.password = "owner-password-ok"
        self.driver.login(self.email, self.password)
        self.room_id = self.driver.create_room(name="War Room")

    def tearDown(self):
        self.driver.close()

    def test_owner_close_returns_303_and_renders_closed_state(self):
        html = self.driver.get(f"/room/{self.room_id}")[1]
        csrf = self.driver.extract_csrf(html)
        status, _, headers = self.driver.post(
            f"/room/{self.room_id}/close", {"_csrf": csrf}
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"/room/{self.room_id}")
        status, body, _ = self.driver.get(f"/room/{self.room_id}")
        self.assertEqual(status, 200)
        self.assertIn("closed", body)


class TestMemberCannotClose(unittest.TestCase):
    """§3.3 + §9.2 #13: a member POST /room/{room_id}/close → 403."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.owner_email = f"owner{time.time_ns()}@example.com"
        self.owner_password = "owner-password-ok"
        self.driver.login(self.owner_email, self.owner_password)
        self.room_id = self.driver.create_room(name="War Room")
        # Invite + accept a member.
        self.member_email = f"member{time.time_ns()}@example.com"
        self.member_password = "member-password-ok"
        csrf = self.driver.csrf()
        self.driver.post(
            "/org/invite",
            {"email": self.member_email, "role": "member", "_csrf": csrf},
        )
        token = re.search(
            r"(fiv_[A-Za-z0-9_-]+)", self.driver.last_outbox_body(self.member_email)
        ).group(1)
        self.driver.post(
            f"/invite/{token}",
            {"email": self.member_email, "password": self.member_password},
        )
        # Log in as the member.
        self.driver.cookies.clear()
        self.driver.post("/login", {"email": self.member_email, "password": self.member_password})
        self.assertIn("fss_session", self.driver.cookies)

    def tearDown(self):
        self.driver.close()

    def test_member_close_returns_403(self):
        html = self.driver.get(f"/room/{self.room_id}")[1]
        csrf = self.driver.extract_csrf(html)
        status, _, _ = self.driver.post(
            f"/room/{self.room_id}/close", {"_csrf": csrf}
        )
        self.assertEqual(status, 403)


class TestMemberCanView(unittest.TestCase):
    """§3.3: a member can VIEW a room and its connect page (view is member+)."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.owner_email = f"owner{time.time_ns()}@example.com"
        self.owner_password = "owner-password-ok"
        self.driver.login(self.owner_email, self.owner_password)
        self.room_id = self.driver.create_room(name="War Room")
        self.member_email = f"member{time.time_ns()}@example.com"
        self.member_password = "member-password-ok"
        csrf = self.driver.csrf()
        self.driver.post(
            "/org/invite",
            {"email": self.member_email, "role": "member", "_csrf": csrf},
        )
        token = re.search(
            r"(fiv_[A-Za-z0-9_-]+)", self.driver.last_outbox_body(self.member_email)
        ).group(1)
        self.driver.post(
            f"/invite/{token}",
            {"email": self.member_email, "password": self.member_password},
        )
        self.driver.cookies.clear()
        self.driver.post("/login", {"email": self.member_email, "password": self.member_password})

    def tearDown(self):
        self.driver.close()

    def test_member_can_view_room_detail(self):
        status, body, _ = self.driver.get(f"/room/{self.room_id}")
        self.assertEqual(status, 200)
        self.assertIn("War Room", body)

    def test_member_can_view_connect_page_with_link_token(self):
        status, body, _ = self.driver.get(f"/room/{self.room_id}/connect")
        self.assertEqual(status, 200)
        self.assertIsNotNone(re.search(r"rm_[A-Za-z0-9_-]+", body))


class TestUnauthenticatedAccess(unittest.TestCase):
    """§3.3 + §9.1: unauthenticated GET /rooms → 303 /login; POST /rooms → 303 /login."""

    def setUp(self):
        self.driver = WebAppDriver()

    def tearDown(self):
        self.driver.close()

    def test_get_rooms_unauthenticated_redirects_to_login(self):
        status, _, headers = self.driver.get("/rooms")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/login"))

    def test_post_rooms_unauthenticated_redirects_to_login(self):
        status, _, headers = self.driver.post("/rooms", {"name": "x", "cap": "8"})
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/login"))


class TestCsrfOnRoomCreate(unittest.TestCase):
    """§9.4 #27: POST /rooms without/with wrong CSRF → 403."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"owner{time.time_ns()}@example.com"
        self.password = "owner-password-ok"
        self.driver.login(self.email, self.password)

    def tearDown(self):
        self.driver.close()

    def test_post_rooms_wrong_csrf_returns_403(self):
        status, _, _ = self.driver.post(
            "/rooms", {"name": "War Room", "cap": "8", "_csrf": "wrong"}
        )
        self.assertEqual(status, 403)


class TestNonexistentRoom(unittest.TestCase):
    """Room detail for a nonexistent room_id (valid member session) → 404."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"owner{time.time_ns()}@example.com"
        self.password = "owner-password-ok"
        self.driver.login(self.email, self.password)

    def tearDown(self):
        self.driver.close()

    def test_nonexistent_room_detail_returns_404(self):
        status, body, _ = self.driver.get("/room/room_deadbeef")
        self.assertEqual(status, 404)


class TestCrossTenantIsolation(unittest.TestCase):
    """§9.3 #17-21: cross-tenant room access → 404 (never 403 — do not reveal existence)."""

    def setUp(self):
        # Org A
        self.driver_a = WebAppDriver()
        self.owner_a = f"ownerA{time.time_ns()}@example.com"
        self.password_a = "owner-password-ok"
        self.driver_a.login(self.owner_a, self.password_a)
        self.room_a = self.driver_a.create_room(name="Room A")
        # Invite + accept a member into org A.
        self.member_a = f"memberA{time.time_ns()}@example.com"
        self.member_a_password = "member-password-ok"
        csrf_a = self.driver_a.csrf()
        self.driver_a.post(
            "/org/invite",
            {"email": self.member_a, "role": "member", "_csrf": csrf_a},
        )
        token = re.search(
            r"(fiv_[A-Za-z0-9_-]+)", self.driver_a.last_outbox_body(self.member_a)
        ).group(1)
        self.driver_a.post(
            f"/invite/{token}",
            {"email": self.member_a, "password": self.member_a_password},
        )
        # Log in as the org-A member (the actor for the cross-tenant probes).
        self.driver_a.cookies.clear()
        self.driver_a.post("/login", {"email": self.member_a, "password": self.member_a_password})
        # Org B — separate driver, separate backend, separate account.
        self.driver_b = WebAppDriver()
        self.owner_b = f"ownerB{time.time_ns()}@example.com"
        self.password_b = "owner-password-ok"
        self.driver_b.login(self.owner_b, self.password_b)
        self.room_b = self.driver_b.create_room(name="Room B")

    def tearDown(self):
        self.driver_a.close()
        self.driver_b.close()

    def test_cross_tenant_room_detail_returns_404(self):
        status, _, _ = self.driver_a.get(f"/room/{self.room_b}")
        self.assertEqual(status, 404)

    def test_cross_tenant_events_returns_404(self):
        status, _, _ = self.driver_a.get(f"/room/{self.room_b}/events?after_seq=0")
        self.assertEqual(status, 404)

    def test_cross_tenant_connect_returns_404(self):
        status, _, _ = self.driver_a.get(f"/room/{self.room_b}/connect")
        self.assertEqual(status, 404)

    def test_cross_tenant_close_returns_404(self):
        html = self.driver_a.get(f"/room/{self.room_b}")[1]
        csrf = self.driver_a.extract_csrf(html) or "x"
        status, _, _ = self.driver_a.post(f"/room/{self.room_b}/close", {"_csrf": csrf})
        self.assertEqual(status, 404)


class TestCsrfOnClose(unittest.TestCase):
    """§9.4 #28: POST /room/{room_id}/close with wrong _csrf → 403 and room NOT closed."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"owner{time.time_ns()}@example.com"
        self.password = "owner-password-ok"
        self.driver.login(self.email, self.password)
        self.room_id = self.driver.create_room(name="War Room")

    def tearDown(self):
        self.driver.close()

    def test_close_with_wrong_csrf_returns_403_and_room_stays_open(self):
        status, _, _ = self.driver.post(
            f"/room/{self.room_id}/close", {"_csrf": "wrong"}
        )
        self.assertEqual(status, 403)
        # Room is NOT closed: detail page still renders active (no 'closed' state).
        status, body, _ = self.driver.get(f"/room/{self.room_id}")
        self.assertEqual(status, 200)
        self.assertNotIn("closed", body)


if __name__ == "__main__":
    unittest.main()
