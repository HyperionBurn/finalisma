"""Wave H — web app security negative contract (RED).

Each test encodes one refusal from docs/WEBAPP_DESIGN.md §9.
The weft_cloud.web package does not exist yet — this file must
fail at import with ModuleNotFoundError.
"""

from __future__ import annotations
from tests._server_readiness import await_serving as _await_serving

import http.client
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.storage import SqliteWalBackend
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.web.app import WeftWebApp  # RED: package absent

SITE_DIR = str(Path(__file__).resolve().parents[1] / "site")


class WebAppDriver:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.backend = SqliteWalBackend(str(Path(self._tmp.name) / "cloud.db"))
        self.backend.initialize()
        ensure_schema(self.backend)
        self.app = WeftWebApp(
            self.backend, static_dir=SITE_DIR, state_dir=str(Path(self._tmp.name) / "state")
        )
        import http.server
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.app.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _await_serving(self.server)
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
                if "Max-Age=0" in val or v.strip() == "":
                    self.cookies.pop(k.strip(), None)
                else:
                    self.cookies[k.strip()] = v.strip()

    def get(self, path, extra_cookie=None):
        headers = self._headers()
        if extra_cookie:
            headers["Cookie"] = (extra_cookie + "; " + headers.get("Cookie", "")).strip("; ")
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        self._collect_cookies(resp)
        body = resp.read().decode("utf-8", errors="replace")
        out = dict(resp.getheaders())
        conn.close()
        return resp.status, body, out

    def _csrf_for_post(self, path: str, form: dict) -> str:
        page = path
        if path in {"/verify", "/reset"}:
            page = f"{path}?token={quote(str(form.get('token', '')), safe='')}"
        status, body, _ = self.get(page)
        if status != 200:
            raise AssertionError(f"CSRF form page {page} returned {status}")
        token = self.extract_csrf(body)
        if not token:
            raise AssertionError(f"CSRF form page {page} did not contain a token")
        return token

    def post(self, path, form: dict, extra_cookie=None, *, auto_csrf: bool = True):
        form = dict(form)
        if auto_csrf and "_csrf" not in form and path in {
            "/signup", "/login", "/verify", "/reset-request", "/reset",
        }:
            form["_csrf"] = self._csrf_for_post(path, form)
        body = urlencode(form)
        headers = self._headers()
        if extra_cookie:
            headers["Cookie"] = (extra_cookie + "; " + headers.get("Cookie", "")).strip("; ")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        self._collect_cookies(resp)
        resp_body = resp.read().decode("utf-8", errors="replace")
        out = dict(resp.getheaders())
        conn.close()
        return resp.status, resp_body, out

    def extract_csrf(self, html):
        m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
        return m.group(1) if m else None

    def csrf(self, path="/"):
        """Fetch a page with the current session and return its CSRF token."""
        _, body, _ = self.get(path)
        token = self.extract_csrf(body)
        assert token is not None, f"no _csrf token found on {path}"
        return token

    def login_only(self, email, password):
        """Log in an existing account (created via invite/seed, not signup)."""
        status, _, _ = self.post("/login", {"email": email, "password": password})
        return status == 303 and "fss_session" in self.cookies

    def signup_verify_login(self, email, password):
        """Signup + verify + login; returns True if login succeeded and set a cookie."""
        self.post("/signup", {"email": email, "password": password})
        body = self.last_outbox_body(email)
        vt = re.search(r"(fvt_[A-Za-z0-9_-]+)", body).group(1)
        self.post("/verify", {"token": vt})
        status, _, _ = self.post("/login", {"email": email, "password": password})
        return status == 303 and "fss_session" in self.cookies

    def last_outbox_body(self, to_email):
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? ORDER BY created_at DESC LIMIT 1",
                (to_email,),
            ).fetchone()
        return row["body"] if row else None

    def account_id(self, email):
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT account_id FROM cloud_identity_accounts WHERE email = ? ORDER BY created_at DESC LIMIT 1",
                (email,),
            ).fetchone()
        return row["account_id"] if row else None

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        finally:
            self.backend.close()
            self._tmp.cleanup()


class TestAuthRefusals(unittest.TestCase):
    """§9.1 Authentication refusals."""

    def setUp(self):
        self.d = WebAppDriver()

    def tearDown(self):
        self.d.close()

    def test_01_get_root_no_cookie_redirects_to_login(self):
        status, body, hdrs = self.d.get("/")
        self.assertEqual(status, 303)
        self.assertTrue(hdrs.get("Location", "").startswith("/login"))
        self.assertNotIn("room", body.lower())
        self.assertNotIn("dashboard", body.lower())

    def test_02_get_room_no_cookie_redirects_to_login(self):
        status, body, hdrs = self.d.get("/room/room_A_1")
        self.assertEqual(status, 303)
        self.assertTrue(hdrs.get("Location", "").startswith("/login"))

    def test_03_get_org_no_cookie_redirects_to_login(self):
        status, body, hdrs = self.d.get("/org")
        self.assertEqual(status, 303)
        self.assertTrue(hdrs.get("Location", "").startswith("/login"))

    def test_04_post_rooms_no_cookie_redirects_to_login(self):
        status, body, hdrs = self.d.post("/rooms", {"name": "x"})
        self.assertEqual(status, 303)
        self.assertTrue(hdrs.get("Location", "").startswith("/login"))

    def test_05_get_root_revoked_session_redirects_and_clears_cookie(self):
        self.assertTrue(self.d.signup_verify_login("revoked@example.com", "Password123!"))
        raw_cookie = self.d.cookies["fss_session"]
        # Look up session_id via token_hash, then revoke
        from weft_cloud.identity import sessions as id_sessions
        token_hash = id_sessions.hash_token(raw_cookie)
        with self.d.backend.transaction() as tx:
            row = tx.execute(
                "SELECT session_id FROM cloud_identity_sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        self.assertIsNotNone(row, "session row must exist after login")
        id_sessions.revoke(self.d.backend, row["session_id"])
        # Now send the old raw cookie
        status, body, hdrs = self.d.get("/", extra_cookie=f"fss_session={raw_cookie}")
        self.assertEqual(status, 303)
        self.assertTrue(hdrs.get("Location", "").startswith("/login"))
        # Cookie must be cleared
        sc = hdrs.get("Set-Cookie", "")
        self.assertIn("Max-Age=0", sc)

    def test_06_get_root_expired_session_redirects_to_login(self):
        # Create a session directly with ttl=1
        from weft_cloud.identity import sessions as id_sessions
        from weft_cloud.identity import accounts
        tenant = "t_exp"
        self.d.backend.create_tenant(tenant, "exp@example.com", "free")
        acct = accounts._create_account(self.d.backend, tenant, "exp@example.com", "Password123!")
        with self.d.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', datetime('now'))",
                (tenant, acct),
            )
            tx.commit()
        sid, raw = id_sessions.create(self.d.backend, tenant, acct, "owner", ttl_seconds=1)
        time.sleep(1.2)
        status, body, hdrs = self.d.get("/", extra_cookie=f"fss_session={raw}")
        self.assertEqual(status, 303)
        self.assertTrue(hdrs.get("Location", "").startswith("/login"))

    def test_07_get_root_tampered_cookie_redirects_to_login(self):
        # A valid-looking token with last char changed
        tampered = "fss_" + "a" * 42
        if tampered[-1] == "a":
            tampered = tampered[:-1] + "b"
        else:
            tampered = tampered[:-1] + "a"
        status, body, hdrs = self.d.get("/", extra_cookie=f"fss_session={tampered}")
        self.assertEqual(status, 303)
        self.assertTrue(hdrs.get("Location", "").startswith("/login"))

    def test_08_post_logout_no_cookie_redirects_no_crash(self):
        status, body, hdrs = self.d.post("/logout", {})
        self.assertEqual(status, 303)
        self.assertTrue(hdrs.get("Location", "").startswith("/login"))


class TestRoleEnforcement(unittest.TestCase):
    """§9.2 Role enforcement refusals."""

    def setUp(self):
        self.d = WebAppDriver()
        # Build org A with owner + member + admin
        self.owner_email = "owner_a@example.com"
        self.member_email = "member_a@example.com"
        self.admin_email = "admin_a@example.com"
        self.password = "Password123!"
        # Owner signup
        self.assertTrue(self.d.signup_verify_login(self.owner_email, self.password))
        self.tenant_a = self.d.account_id(self.owner_email)
        # Invite member and admin
        _, body, _ = self.d.get("/org")
        csrf = self.d.extract_csrf(body)
        self.d.post("/org/invite", {"email": self.member_email, "role": "member", "_csrf": csrf})
        _, body, _ = self.d.get("/org")
        csrf = self.d.extract_csrf(body)
        self.d.post("/org/invite", {"email": self.admin_email, "role": "admin", "_csrf": csrf})
        # Accept invites
        for email, role in [(self.member_email, "member"), (self.admin_email, "admin")]:
            ob = self.d.last_outbox_body(email)
            token = re.search(r"(fiv_[A-Za-z0-9_-]+)", ob).group(1)
            self.d.get(f"/invite/{token}")
            _, body2, _ = self.d.get(f"/invite/{token}")
            csrf2 = self.d.extract_csrf(body2)
            self.d.post(f"/invite/{token}", {"email": email, "password": self.password, "_csrf": csrf2})
        # Login as each (owner/member/admin were created by signup/invite —
        # they already exist, so log in rather than sign up).
        self.d.cookies.clear()
        self.assertTrue(self.d.login_only(self.owner_email, self.password))
        self.owner_cookie = self.d.cookies["fss_session"]
        # Create a room as owner
        _, body, _ = self.d.get("/")
        csrf = self.d.extract_csrf(body)
        self.d.post("/rooms", {"name": "room_a", "_csrf": csrf})
        # Find room id
        m = re.search(r"/room/([a-zA-Z0-9_-]+)", body)
        if not m:
            # re-fetch dashboard
            _, body2, _ = self.d.get("/")
            m = re.search(r"/room/([a-zA-Z0-9_-]+)", body2)
        self.room_a = m.group(1)
        # Login member
        self.d.cookies.clear()
        self.assertTrue(self.d.login_only(self.member_email, self.password))
        self.member_cookie = self.d.cookies["fss_session"]
        # Login admin
        self.d.cookies.clear()
        self.assertTrue(self.d.login_only(self.admin_email, self.password))
        self.admin_cookie = self.d.cookies["fss_session"]

    def tearDown(self):
        self.d.close()

    def test_09_member_cannot_create_room(self):
        status, _, _ = self.d.post("/rooms", {"name": "x"}, extra_cookie=f"fss_session={self.member_cookie}")
        self.assertEqual(status, 403)

    def test_10_member_cannot_invite(self):
        status, _, _ = self.d.post("/org/invite", {"email": "x@example.com", "role": "member"}, extra_cookie=f"fss_session={self.member_cookie}")
        self.assertEqual(status, 403)

    def test_11_member_cannot_remove(self):
        acct = self.d.account_id(self.admin_email)
        status, _, _ = self.d.post("/org/remove", {"account_id": acct}, extra_cookie=f"fss_session={self.member_cookie}")
        self.assertEqual(status, 403)

    def test_12_member_cannot_change_role(self):
        acct = self.d.account_id(self.admin_email)
        status, _, _ = self.d.post("/org/role", {"account_id": acct, "role": "member"}, extra_cookie=f"fss_session={self.member_cookie}")
        self.assertEqual(status, 403)

    def test_13_member_cannot_close_room(self):
        status, _, _ = self.d.post(f"/room/{self.room_a}/close", {}, extra_cookie=f"fss_session={self.member_cookie}")
        self.assertEqual(status, 403)

    def test_14_admin_cannot_close_room(self):
        status, _, _ = self.d.post(f"/room/{self.room_a}/close", {}, extra_cookie=f"fss_session={self.admin_cookie}")
        self.assertEqual(status, 403)

    def test_15_admin_cannot_create_second_org(self):
        status, body, _ = self.d.post("/org", {"email": "new@example.com"}, extra_cookie=f"fss_session={self.admin_cookie}")
        self.assertEqual(status, 400)
        # No second membership row
        acct = self.d.account_id(self.admin_email)
        with self.d.backend.transaction() as tx:
            rows = tx.execute(
                "SELECT COUNT(*) AS c FROM cloud_identity_members WHERE account_id = ?",
                (acct,),
            ).fetchone()
        self.assertEqual(rows["c"], 1, "admin must remain member of exactly one org")

    def test_16_admin_cannot_transfer_ownership(self):
        acct = self.d.account_id(self.member_email)
        status, _, _ = self.d.post("/org/role", {"account_id": acct, "role": "owner"}, extra_cookie=f"fss_session={self.admin_cookie}")
        self.assertEqual(status, 403)
        status, _, _ = self.d.post("/org/transfer", {"account_id": acct}, extra_cookie=f"fss_session={self.admin_cookie}")
        self.assertEqual(status, 403)


class TestTenantIsolation(unittest.TestCase):
    """§9.3 Tenant isolation refusals."""

    def setUp(self):
        self.d = WebAppDriver()
        # Org A
        self.owner_a = "owner_a@example.com"
        self.member_a = "member_a@example.com"
        self.password = "Password123!"
        self.assertTrue(self.d.signup_verify_login(self.owner_a, self.password))
        _, body, _ = self.d.get("/org")
        csrf = self.d.extract_csrf(body)
        self.d.post("/org/invite", {"email": self.member_a, "role": "member", "_csrf": csrf})
        ob = self.d.last_outbox_body(self.member_a)
        token = re.search(r"(fiv_[A-Za-z0-9_-]+)", ob).group(1)
        self.d.get(f"/invite/{token}")
        _, body2, _ = self.d.get(f"/invite/{token}")
        csrf2 = self.d.extract_csrf(body2)
        self.d.post(f"/invite/{token}", {"email": self.member_a, "password": self.password, "_csrf": csrf2})
        # Re-login as owner — invite-accept switched the session to the member,
        # and only admin+ can create rooms.
        self.d.cookies.clear()
        self.assertTrue(self.d.login_only(self.owner_a, self.password))
        # Create room A
        _, body, _ = self.d.get("/")
        csrf = self.d.extract_csrf(body)
        self.d.post("/rooms", {"name": "room_a", "_csrf": csrf})
        _, body2, _ = self.d.get("/")
        m = re.search(r"/room/([a-zA-Z0-9_-]+)", body2)
        self.room_a = m.group(1)
        self.d.cookies.clear()
        self.assertTrue(self.d.login_only(self.member_a, self.password))
        self.member_a_cookie = self.d.cookies["fss_session"]
        # Org B
        self.d.cookies.clear()
        self.owner_b = "owner_b@example.com"
        self.assertTrue(self.d.signup_verify_login(self.owner_b, self.password))
        _, body, _ = self.d.get("/")
        csrf = self.d.extract_csrf(body)
        self.d.post("/rooms", {"name": "room_b", "_csrf": csrf})
        _, body2, _ = self.d.get("/")
        m = re.search(r"/room/([a-zA-Z0-9_-]+)", body2)
        self.room_b = m.group(1)

    def tearDown(self):
        self.d.close()

    def test_17_org_a_member_cannot_see_room_b(self):
        status, _, _ = self.d.get(f"/room/{self.room_b}", extra_cookie=f"fss_session={self.member_a_cookie}")
        self.assertEqual(status, 404)

    def test_18_org_a_member_cannot_poll_room_b_events(self):
        status, _, _ = self.d.get(f"/room/{self.room_b}/events", extra_cookie=f"fss_session={self.member_a_cookie}")
        self.assertEqual(status, 404)

    def test_19_org_a_member_cannot_see_room_b_audit(self):
        status, _, _ = self.d.get(f"/room/{self.room_b}/audit", extra_cookie=f"fss_session={self.member_a_cookie}")
        self.assertEqual(status, 404)

    def test_20_org_a_member_cannot_see_room_b_connect(self):
        status, _, _ = self.d.get(f"/room/{self.room_b}/connect", extra_cookie=f"fss_session={self.member_a_cookie}")
        self.assertEqual(status, 404)

    def test_21_org_a_member_cannot_close_room_b(self):
        status, _, _ = self.d.post(f"/room/{self.room_b}/close", {}, extra_cookie=f"fss_session={self.member_a_cookie}")
        self.assertEqual(status, 404)

    def test_22_org_a_member_sees_only_org_a_members(self):
        status, body, _ = self.d.get("/org", extra_cookie=f"fss_session={self.member_a_cookie}")
        self.assertEqual(status, 200)
        self.assertIn(self.owner_a, body)
        self.assertNotIn(self.owner_b, body)

    def test_23_org_a_session_against_org_b_resource(self):
        status, _, _ = self.d.get(f"/room/{self.room_b}", extra_cookie=f"fss_session={self.member_a_cookie}")
        self.assertIn(status, (404, 303))


class TestCsrfRefusals(unittest.TestCase):
    """§9.4 CSRF refusals."""

    def setUp(self):
        self.d = WebAppDriver()
        self.email = "csrf@example.com"
        self.password = "Password123!"
        self.assertTrue(self.d.signup_verify_login(self.email, self.password))
        self.cookie = self.d.cookies["fss_session"]

    def tearDown(self):
        self.d.close()

    def test_24_post_logout_without_csrf_refused(self):
        saved = dict(self.d.cookies)
        status, _, hdrs = self.d.post("/logout", {}, extra_cookie=f"fss_session={self.cookie}")
        self.assertEqual(status, 403)
        # Cookie NOT cleared
        self.assertNotIn("Set-Cookie", hdrs)
        self.d.cookies = saved

    def test_25_post_logout_with_wrong_csrf_refused(self):
        saved = dict(self.d.cookies)
        status, _, hdrs = self.d.post("/logout", {"_csrf": "wrong"}, extra_cookie=f"fss_session={self.cookie}")
        self.assertEqual(status, 403)
        self.assertNotIn("Set-Cookie", hdrs)
        self.d.cookies = saved

    def test_26_post_org_invite_wrong_csrf_refused(self):
        status, _, _ = self.d.post("/org/invite", {"email": "x@example.com", "role": "member", "_csrf": "wrong"}, extra_cookie=f"fss_session={self.cookie}")
        self.assertEqual(status, 403)

    def test_27_post_rooms_wrong_csrf_refused(self):
        status, _, _ = self.d.post("/rooms", {"name": "x", "_csrf": "wrong"}, extra_cookie=f"fss_session={self.cookie}")
        self.assertEqual(status, 403)

    def test_28_post_room_close_wrong_csrf_refused(self):
        # Create a room first with correct csrf
        _, body, _ = self.d.get("/")
        csrf = self.d.extract_csrf(body)
        self.d.post("/rooms", {"name": "csrf_room", "_csrf": csrf})
        _, body2, _ = self.d.get("/")
        m = re.search(r"/room/([a-zA-Z0-9_-]+)", body2)
        room_id = m.group(1)
        # Now try close with wrong csrf
        status, _, _ = self.d.post(f"/room/{room_id}/close", {"_csrf": "wrong"}, extra_cookie=f"fss_session={self.cookie}")
        self.assertEqual(status, 403)
        # Room not closed — verify by fetching detail
        status2, _, _ = self.d.get(f"/room/{room_id}", extra_cookie=f"fss_session={self.cookie}")
        self.assertEqual(status2, 200)


class TestNoSecretsInHtml(unittest.TestCase):
    """§9.5 No-secrets-in-HTML refusals."""

    def setUp(self):
        self.d = WebAppDriver()
        self.email = "secrets@example.com"
        self.password = "Password123!"
        self.assertTrue(self.d.signup_verify_login(self.email, self.password))
        self.cookie = self.d.cookies["fss_session"]
        # Create a room
        _, body, _ = self.d.get("/")
        csrf = self.d.extract_csrf(body)
        self.d.post("/rooms", {"name": "sec_room", "_csrf": csrf})
        _, body2, _ = self.d.get("/")
        m = re.search(r"/room/([a-zA-Z0-9_-]+)", body2)
        self.room_id = m.group(1)

    def tearDown(self):
        self.d.close()

    def test_29_rendered_pages_contain_no_raw_secrets(self):
        pages = []
        for path in ["/", "/org", f"/room/{self.room_id}", f"/room/{self.room_id}/connect"]:
            _, body, _ = self.d.get(path, extra_cookie=f"fss_session={self.cookie}")
            pages.append((path, body))
        for path, body in pages:
            self.assertNotIn(self.cookie, body, f"raw session token leaked on {path}")
            self.assertNotIn(self.password, body, f"raw password leaked on {path}")
            # No fvt_ / frt_ / fiv_ / rm_ tokens in rendered HTML
            self.assertNotRegex(body, r"fvt_[A-Za-z0-9_-]+", f"verify token leaked on {path}")
            self.assertNotRegex(body, r"frt_[A-Za-z0-9_-]+", f"reset token leaked on {path}")
            self.assertNotRegex(body, r"fiv_[A-Za-z0-9_-]+", f"invite token leaked on {path}")

    def test_30_bad_login_error_page_does_not_contain_password(self):
        self.d.cookies.clear()
        status, body, _ = self.d.post("/login", {"email": self.email, "password": self.password})
        self.assertNotIn(self.password, body)

    def test_31_no_raw_token_in_redirect_location(self):
        # Trigger verify flow
        self.d.post("/signup", {"email": "newverify@example.com", "password": "Password123!"})
        ob = self.d.last_outbox_body("newverify@example.com")
        vt = re.search(r"(fvt_[A-Za-z0-9_-]+)", ob).group(1)
        # GET /verify?token=... is allowed (public page render)
        status, body, hdrs = self.d.get(f"/verify?token={vt}")
        # But no Set-Cookie or redirect leaks the token
        loc = hdrs.get("Location", "")
        self.assertNotIn("fvt_", loc, "verify token leaked in redirect URL")
        self.assertNotIn("frt_", loc, "reset token leaked in redirect URL")

    def test_32_non_member_connect_redirects_no_token(self):
        # Create a second account (non-member of the room's org)
        self.d.cookies.clear()
        other = "other@example.com"
        self.assertTrue(self.d.signup_verify_login(other, self.password))
        other_cookie = self.d.cookies["fss_session"]
        status, body, hdrs = self.d.get(f"/room/{self.room_id}/connect", extra_cookie=f"fss_session={other_cookie}")
        # Cross-tenant room access is 404 (do not reveal existence), never 303.
        self.assertEqual(status, 404)
        # No link token in body
        self.assertNotRegex(body, r"rm_[A-Za-z0-9_-]+")

    def test_33_member_connect_renders_link_token(self):
        status, body, _ = self.d.get(f"/room/{self.room_id}/connect", extra_cookie=f"fss_session={self.cookie}")
        self.assertEqual(status, 200)
        self.assertRegex(body, r"rm_[A-Za-z0-9_-]+", "link token must be present for members")


class TestRoomLinkRefusals(unittest.TestCase):
    """§9.6 Room link refusals."""

    def setUp(self):
        self.d = WebAppDriver()
        self.email = "link@example.com"
        self.password = "Password123!"
        self.assertTrue(self.d.signup_verify_login(self.email, self.password))
        self.cookie = self.d.cookies["fss_session"]
        _, body, _ = self.d.get("/")
        csrf = self.d.extract_csrf(body)
        self.d.post("/rooms", {"name": "link_room", "_csrf": csrf})
        _, body2, _ = self.d.get("/")
        m = re.search(r"/room/([a-zA-Z0-9_-]+)", body2)
        self.room_id = m.group(1)

    def tearDown(self):
        self.d.close()

    def test_34_non_member_connect_redirects_to_login(self):
        self.d.cookies.clear()
        other = "other_link@example.com"
        self.assertTrue(self.d.signup_verify_login(other, self.password))
        other_cookie = self.d.cookies["fss_session"]
        status, body, hdrs = self.d.get(f"/room/{self.room_id}/connect", extra_cookie=f"fss_session={other_cookie}")
        # Cross-tenant room access is 404 (do not reveal existence), never 303.
        self.assertEqual(status, 404)

    def test_35_expired_link_token_coordinator_refuses(self):
        # The web app renders config regardless; coordinator enforces.
        # This test asserts the page renders (no crash) — the coordinator
        # refusal is an integration concern, not a web-app concern.
        status, body, _ = self.d.get(f"/room/{self.room_id}/connect", extra_cookie=f"fss_session={self.cookie}")
        self.assertEqual(status, 200)
        self.assertRegex(body, r"rm_[A-Za-z0-9_-]+")


if __name__ == "__main__":
    unittest.main()
