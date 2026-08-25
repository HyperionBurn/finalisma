"""Wave H — web org/member routes integration contract (RED).

The weft_cloud.web package does not exist yet — this file must fail at
import with ModuleNotFoundError.

Drives real HTTP against an in-process WeftWebApp server. Route contract
from docs/WEBAPP_DESIGN.md sections 3.1, 3.2, 4, 8, 9.2, 9.3, 10.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.storage import SqliteWalBackend
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.identity.agent_keys import create as create_agent_key, validate as validate_agent_key
from weft_cloud.identity.sessions import AuthError, create as create_session, validate as validate_session
from weft_cloud.web.app import WeftWebApp  # RED: package absent

SITE_DIR = str(ROOT / "site")


class WebAppDriver:
    """In-process HTTP driver for WeftWebApp.

    Contract: WeftWebApp(backend, static_dir=..., state_dir=...)
    exposes .handler (a BaseHTTPRequestHandler subclass) and is driven on
    ("127.0.0.1", 0) with finally teardown.
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

    def post(self, path, form: dict, *, auto_csrf: bool = True):
        form = dict(form)
        if auto_csrf and "_csrf" not in form and path in {
            "/signup", "/login", "/verify", "/reset-request", "/reset",
        }:
            form["_csrf"] = self._csrf_for_post(path, form)
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

    def accept_invite(self, token, email, password):
        """Accept an invite through its form, including the public CSRF token."""
        status, body, _ = self.get(f"/invite/{token}")
        assert status == 200, f"invite form expected 200, got {status}"
        csrf = self.extract_csrf(body)
        assert csrf is not None, "invite form did not include _csrf"
        return self.post(
            f"/invite/{token}",
            {"email": email, "password": password, "_csrf": csrf},
        )

    def last_outbox_body(self, to_email):
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (to_email,),
            ).fetchone()
        return row["body"] if row else None

    def account_id_for_email(self, tenant_id, email):
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT account_id FROM cloud_identity_accounts "
                "WHERE tenant_id = ? AND email = ?",
                (tenant_id, email),
            ).fetchone()
        return row["account_id"] if row else None

    def tenant_for_email(self, email):
        """Return the tenant_id for the org whose owner has this email, or None."""
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT m.tenant_id FROM cloud_identity_members m "
                "JOIN cloud_identity_accounts a ON a.account_id = m.account_id "
                "WHERE a.email = ? AND m.role = 'owner'",
                (email,),
            ).fetchone()
        return row["tenant_id"] if row else None

    def membership_count(self, account_id):
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT COUNT(*) AS c FROM cloud_identity_members WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return row["c"] if row else 0

    def login(self, email, password):
        """Sign up + verify + login as a fresh user.

        Returns (status_of_login, cookie_set). The signup POST includes a
        _csrf key which the public signup route ignores (pre-auth).
        """
        self.post("/signup", {"email": email, "password": password})
        body = self.last_outbox_body(email)
        token = re.search(r"(fvt_[A-Za-z0-9_-]+)", body).group(1)
        self.post("/verify", {"token": token})
        status, _, _ = self.post("/login", {"email": email, "password": password})
        return status, "fss_session" in self.cookies

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        finally:
            self.backend.close()
            self._tmp.cleanup()


class TestOrgCreationAndMembership(unittest.TestCase):
    """§3.2 + §8: signup creates an org whose owner is the signing-up user."""

    def setUp(self):
        self.driver = WebAppDriver()

    def tearDown(self):
        self.driver.close()

    def test_signup_creates_org_with_single_owner_member(self):
        email = f"owner{time.time_ns()}@example.com"
        password = "owner-password-ok"
        status, cookie_set = self.driver.login(email, password)
        self.assertEqual(status, 303)
        self.assertTrue(cookie_set)
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        # The owner's email appears in the members list.
        self.assertIn(email, body)
        # The role 'owner' renders for the owner's row.
        self.assertIn("owner", body)
        # Exactly one membership row for this account.
        tenant_id = self.driver.tenant_for_email(email)
        self.assertIsNotNone(tenant_id)
        acct = self.driver.account_id_for_email(tenant_id, email)
        self.assertEqual(self.driver.membership_count(acct), 1)

    def test_org_page_wraps_member_table_on_narrow_viewports(self):
        email = f"owner{time.time_ns()}@example.com"
        self.driver.login(email, "owner-password-ok")
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        self.assertIn("width:100%", body)
        self.assertIn("overflow-wrap:anywhere", body)

    def test_unauthenticated_get_org_redirects_to_login(self):
        status, _, headers = self.driver.get("/org")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/login"))


class TestInviteFlow(unittest.TestCase):
    """§3.2: owner can invite; invite-accept adds a member to the org."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.owner_email = f"owner{time.time_ns()}@example.com"
        self.owner_password = "owner-password-ok"
        self.driver.login(self.owner_email, self.owner_password)
        self.tenant_id = self.driver.tenant_for_email(self.owner_email)

    def tearDown(self):
        self.driver.close()

    def test_owner_invite_creates_fiv_token_and_redirects(self):
        invitee_email = f"invitee{time.time_ns()}@example.com"
        csrf = self.driver.csrf()
        status, _, headers = self.driver.post(
            "/org/invite",
            {"email": invitee_email, "role": "member", "_csrf": csrf},
        )
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/org"))
        outbox_body = self.driver.last_outbox_body(invitee_email)
        self.assertIsNotNone(outbox_body)
        m = re.search(r"(fiv_[A-Za-z0-9_-]+)", outbox_body)
        self.assertIsNotNone(m, "fiv_ token must appear in outbox body")
        self._invite_token = m.group(1)

    def test_invite_accept_flow_adds_member(self):
        invitee_email = f"invitee{time.time_ns()}@example.com"
        invitee_password = "invitee-password-ok"
        # Owner invites (authenticated → real CSRF token).
        csrf = self.driver.csrf()
        self.driver.post(
            "/org/invite",
            {"email": invitee_email, "role": "member", "_csrf": csrf},
        )
        token = re.search(
            r"(fiv_[A-Za-z0-9_-]+)", self.driver.last_outbox_body(invitee_email)
        ).group(1)
        # GET accept form.
        status, body, _ = self.driver.get(f"/invite/{token}")
        self.assertEqual(status, 200)
        self.assertIn('name="email"', body)
        self.assertIn('name="password"', body)
        invite_csrf = self.driver.extract_csrf(body)
        self.assertIsNotNone(invite_csrf)
        session_before = self.driver.cookies.get("fss_session")
        # Missing and wrong CSRF tokens must not consume the invite or issue a
        # new session; the same invite remains available for the valid submit.
        status, _, _ = self.driver.post(
            f"/invite/{token}",
            {"email": invitee_email, "password": invitee_password},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.driver.cookies.get("fss_session"), session_before)
        status, _, _ = self.driver.post(
            f"/invite/{token}",
            {"email": invitee_email, "password": invitee_password, "_csrf": "wrong"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.driver.cookies.get("fss_session"), session_before)
        # POST accept with the form token.
        status, _, headers = self.driver.post(
            f"/invite/{token}",
            {"email": invitee_email, "password": invitee_password, "_csrf": invite_csrf},
        )
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/"))
        self.assertIn("fss_session", self.driver.cookies)
        # Log the new member in via that session; GET /org shows their email.
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        self.assertIn(invitee_email, body)


class TestRoleManagement(unittest.TestCase):
    """§3.2: owner can set role; role change is real (defence in depth)."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.owner_email = f"owner{time.time_ns()}@example.com"
        self.owner_password = "owner-password-ok"
        self.driver.login(self.owner_email, self.owner_password)
        self.tenant_id = self.driver.tenant_for_email(self.owner_email)
        # Invite + accept a second member.
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
        self.driver.accept_invite(token, self.member_email, self.member_password)
        # Re-login as owner (invite-accept set the session to the new member).
        self.driver.cookies.clear()
        self.driver.post("/login", {"email": self.owner_email, "password": self.owner_password})
        self.member_acct = self.driver.account_id_for_email(self.tenant_id, self.member_email)
        self.assertIsNotNone(self.member_acct)

    def tearDown(self):
        self.driver.close()

    def test_owner_can_promote_member_to_admin(self):
        csrf = self.driver.csrf()
        status, _, headers = self.driver.post(
            "/org/role",
            {"account_id": self.member_acct, "role": "admin", "_csrf": csrf},
        )
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/org"))
        # Role change is real: the promoted member can now invite.
        # Log in as the member.
        self.driver.cookies.clear()
        self.driver.post("/login", {"email": self.member_email, "password": self.member_password})
        new_invitee = f"late{time.time_ns()}@example.com"
        csrf = self.driver.csrf()
        status, _, headers = self.driver.post(
            "/org/invite",
            {"email": new_invitee, "role": "member", "_csrf": csrf},
        )
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/org"))

    def test_owner_can_choose_member_by_email_instead_of_copying_account_id(self):
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        self.assertIn('<label>Member <select name="account_id"', body)
        self.assertIn(self.member_email, body)

    def test_admin_does_not_see_impossible_owner_controls(self):
        csrf = self.driver.csrf()
        status, _, _ = self.driver.post(
            "/org/role",
            {"account_id": self.member_acct, "role": "admin", "_csrf": csrf},
        )
        self.assertEqual(status, 303)
        owner_acct = self.driver.account_id_for_email(self.tenant_id, self.owner_email)
        self.driver.cookies.clear()
        self.driver.post("/login", {"email": self.member_email, "password": self.member_password})
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        role_form = re.search(
            r'<form method="post" action="/org/role">(.*?)</form>', body, re.DOTALL
        ).group(1)
        self.assertNotIn(f'value="{owner_acct}"', role_form)
        self.assertNotIn('<option value="owner">owner</option>', role_form)
        remove_match = re.search(
            r'<form method="post" action="/org/remove">(.*?)</form>', body, re.DOTALL
        )
        if remove_match:
            self.assertNotIn(f'value="{owner_acct}"', remove_match.group(1))

    def test_owner_can_transfer_ownership_and_then_leave(self):
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        self.assertIn('action="/org/transfer"', body)
        self.assertIn(f"New owner", body)
        self.assertIn(self.member_email, body)
        role_form = re.search(
            r'<form method="post" action="/org/role">(.*?)</form>', body, re.DOTALL
        ).group(1)
        self.assertNotIn('<option value="owner">owner</option>', role_form)

        csrf = self.driver.csrf("/org")
        status, _, headers = self.driver.post(
            "/org/transfer",
            {"account_id": self.member_acct, "_csrf": csrf},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login?ownership_transferred=1")
        self.assertNotIn("fss_session", self.driver.cookies)

        status, body, _ = self.driver.get("/login?ownership_transferred=1")
        self.assertEqual(status, 200)
        self.assertIn("Ownership was transferred", body)
        status, _, headers = self.driver.post(
            "/login", {"email": self.owner_email, "password": self.owner_password}
        )
        self.assertEqual(status, 303)
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        self.assertIn(f"{self.owner_email}", body)
        self.assertIn(f"{self.member_email}", body)
        member_row = re.search(
            rf"<tr><td>{re.escape(self.member_email)}</td><td>([^<]+)</td>", body
        )
        owner_row = re.search(
            rf"<tr><td>{re.escape(self.owner_email)}</td><td>([^<]+)</td>", body
        )
        self.assertIsNotNone(member_row)
        self.assertIsNotNone(owner_row)
        self.assertEqual(member_row.group(1), "owner")
        self.assertEqual(owner_row.group(1), "admin")

        csrf = self.driver.csrf("/org")
        status, _, headers = self.driver.post("/org/leave", {"_csrf": csrf})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login")
        self.assertNotIn("fss_session", self.driver.cookies)


class TestRemoveMember(unittest.TestCase):
    """§3.2: owner can remove a member; removed member's session is gone."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.owner_email = f"owner{time.time_ns()}@example.com"
        self.owner_password = "owner-password-ok"
        self.driver.login(self.owner_email, self.owner_password)
        self.tenant_id = self.driver.tenant_for_email(self.owner_email)
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
        self.driver.accept_invite(token, self.member_email, self.member_password)
        self.member_acct = self.driver.account_id_for_email(self.tenant_id, self.member_email)
        # Log in as the member to establish their session.
        self.member_cookies = dict(self.driver.cookies)
        # Re-login as owner.
        self.driver.cookies.clear()
        self.driver.post("/login", {"email": self.owner_email, "password": self.owner_password})

    def tearDown(self):
        self.driver.close()

    def test_owner_can_remove_member(self):
        csrf = self.driver.csrf()
        status, _, headers = self.driver.post(
            "/org/remove",
            {"account_id": self.member_acct, "_csrf": csrf},
        )
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/org"))
        # After removal, the member's session is gone → GET / redirects to /login.
        saved = dict(self.driver.cookies)
        self.driver.cookies.clear()
        self.driver.cookies.update(self.member_cookies)
        status, _, headers = self.driver.get("/")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/login"))
        self.driver.cookies.clear()
        self.driver.cookies.update(saved)


class TestOwnerLeave(unittest.TestCase):
    """§3.2: owner leave is refused while other members exist."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.owner_email = f"owner{time.time_ns()}@example.com"
        self.owner_password = "owner-password-ok"
        self.driver.login(self.owner_email, self.owner_password)
        self.tenant_id = self.driver.tenant_for_email(self.owner_email)
        # Add a second member.
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
        self.driver.accept_invite(token, self.member_email, self.member_password)
        # Re-login as owner.
        self.driver.cookies.clear()
        self.driver.post("/login", {"email": self.owner_email, "password": self.owner_password})

    def tearDown(self):
        self.driver.close()

    def test_owner_leave_refused_while_other_members_exist(self):
        csrf = self.driver.csrf()
        status, body, _ = self.driver.post("/org/leave", {"_csrf": csrf})
        self.assertIn(status, (400, 403))
        self.assertIn("Owners cannot leave", body)
        # Org still exists.
        self.assertIsNotNone(self.driver.backend.get_tenant(self.tenant_id))

    def test_owner_org_page_explains_that_leave_is_unavailable(self):
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        self.assertNotIn('action="/org/leave"', body)
        self.assertIn("Owners cannot leave an organization", body)


class TestOwnerLeaveAlone(unittest.TestCase):
    """Owner leave is a truthful refusal even when no other member exists."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.owner_email = f"owner{time.time_ns()}@example.com"
        self.driver.login(self.owner_email, "owner-password-ok")
        self.tenant_id = self.driver.tenant_for_email(self.owner_email)
        self.owner_acct = self.driver.account_id_for_email(self.tenant_id, self.owner_email)

    def tearDown(self):
        self.driver.close()

    def test_owner_leave_refused_truthfully_and_membership_remains(self):
        csrf = self.driver.csrf()
        status, body, _ = self.driver.post("/org/leave", {"_csrf": csrf})
        self.assertEqual(status, 400)
        self.assertIn("Owners cannot leave", body)
        self.assertEqual(self.driver.membership_count(self.owner_acct), 1)
        self.assertIsNotNone(self.driver.backend.get_tenant(self.tenant_id))


class TestOwnerDeleteOrganization(unittest.TestCase):
    """Owner deletion is explicit, atomic, and visible in the browser flow."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.owner_email = f"delete-owner{time.time_ns()}@example.com"
        self.owner_password = "owner-password-ok"
        self.driver.login(self.owner_email, self.owner_password)
        self.tenant_id = self.driver.tenant_for_email(self.owner_email)
        self.owner_acct = self.driver.account_id_for_email(self.tenant_id, self.owner_email)
        self.key_id, self.raw_key = create_agent_key(
            self.driver.backend, self.tenant_id, self.owner_acct, "delete-me"
        )
        self.room = self.driver.app.rooms.create_room(
            self.tenant_id,
            self.owner_acct,
            self.driver.cookies["fss_session"],
            cap=2,
            name="delete-me",
            actor_account_id=self.owner_acct,
        )

    def tearDown(self):
        self.driver.close()

    def test_delete_requires_exact_confirmation_and_removes_all_tenant_state(self):
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        self.assertIn('action="/org/delete"', body)
        self.assertIn("Type DELETE to confirm", body)
        self.assertIn("This is permanent", body)

        csrf = self.driver.csrf("/org")
        status, body, _ = self.driver.post(
            "/org/delete", {"_csrf": csrf, "confirmation": "delete"}
        )
        self.assertEqual(status, 400)
        self.assertIn("Type DELETE exactly to confirm", body)
        self.assertIsNotNone(self.driver.backend.get_tenant(self.tenant_id))

        # A forged request without the session's CSRF token must not delete.
        status, _, _ = self.driver.post(
            "/org/delete", {"confirmation": "DELETE"}, auto_csrf=False
        )
        self.assertEqual(status, 403)
        self.assertIsNotNone(self.driver.backend.get_tenant(self.tenant_id))

        csrf = self.driver.csrf("/org")
        status, _, headers = self.driver.post(
            "/org/delete", {"_csrf": csrf, "confirmation": "DELETE"}
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login?org_deleted=1")
        self.assertNotIn("fss_session", self.driver.cookies)
        self.assertIsNone(self.driver.backend.get_tenant(self.tenant_id))

        with self.driver.backend.transaction() as tx:
            for table in (
                "cloud_identity_accounts",
                "cloud_identity_members",
                "cloud_identity_agent_keys",
                "cloud_rooms",
                "cloud_room_members",
                "cloud_room_links",
                "cloud_room_event_log",
                "cloud_room_counters",
            ):
                remaining = tx.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id = ?",
                    (self.tenant_id,),
                ).fetchone()["n"]
                self.assertEqual(remaining, 0, table)

        status, body, _ = self.driver.get("/login?org_deleted=1")
        self.assertEqual(status, 200)
        self.assertIn("organization and its data were permanently deleted", body)


class TestMemberLeave(unittest.TestCase):
    """A non-owner leave removes membership and tenant-scoped credentials."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.owner_email = f"owner{time.time_ns()}@example.com"
        self.driver.login(self.owner_email, "owner-password-ok")
        self.tenant_id = self.driver.tenant_for_email(self.owner_email)
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
        self.driver.accept_invite(token, self.member_email, self.member_password)
        self.member_cookies = dict(self.driver.cookies)
        self.member_token = self.member_cookies["fss_session"]
        self.member_acct = self.driver.account_id_for_email(self.tenant_id, self.member_email)
        self.key_id, self.agent_key = create_agent_key(
            self.driver.backend, self.tenant_id, self.member_acct, "member-laptop"
        )
        _, self.second_session = create_session(
            self.driver.backend, self.tenant_id, self.member_acct, "member"
        )

    def tearDown(self):
        self.driver.close()

    def test_member_leave_removes_membership_revokes_credentials_and_redirects(self):
        self.driver.cookies.clear()
        self.driver.cookies.update(self.member_cookies)
        csrf = self.driver.csrf("/org")
        status, _, headers = self.driver.post("/org/leave", {"_csrf": csrf})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login")
        self.assertNotIn("fss_session", self.driver.cookies)

        with self.driver.backend.transaction() as tx:
            membership = tx.execute(
                "SELECT 1 FROM cloud_identity_members WHERE tenant_id = ? AND account_id = ?",
                (self.tenant_id, self.member_acct),
            ).fetchone()
            key = tx.execute(
                "SELECT revoked_at FROM cloud_identity_agent_keys WHERE tenant_id = ? AND key_id = ?",
                (self.tenant_id, self.key_id),
            ).fetchone()
            sessions = tx.execute(
                "SELECT COUNT(*) AS n FROM cloud_identity_sessions "
                "WHERE tenant_id = ? AND account_id = ? AND revoked_at IS NULL",
                (self.tenant_id, self.member_acct),
            ).fetchone()
        self.assertIsNone(membership)
        self.assertIsNotNone(key["revoked_at"])
        self.assertEqual(sessions["n"], 0)
        with self.assertRaises(AuthError):
            validate_session(self.driver.backend, self.member_token)
        with self.assertRaises(AuthError):
            validate_session(self.driver.backend, self.second_session)
        with self.assertRaises(AuthError):
            validate_agent_key(self.driver.backend, self.agent_key)

    def test_non_owner_with_active_room_cannot_leave(self):
        self.driver.app.rooms.create_room(
            self.tenant_id,
            self.member_acct,
            "member-room-actor-token-1234",
            name="member-owned-room",
        )
        self.driver.cookies.clear()
        self.driver.cookies.update(self.member_cookies)
        csrf = self.driver.csrf("/org")
        status, body, _ = self.driver.post("/org/leave", {"_csrf": csrf})
        self.assertEqual(status, 400)
        self.assertIn("active room", body)
        with self.driver.backend.transaction() as tx:
            membership = tx.execute(
                "SELECT 1 FROM cloud_identity_members WHERE tenant_id = ? AND account_id = ?",
                (self.tenant_id, self.member_acct),
            ).fetchone()
        self.assertIsNotNone(membership)

    def test_member_cannot_see_or_post_organization_delete(self):
        self.driver.cookies.clear()
        self.driver.cookies.update(self.member_cookies)
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        self.assertNotIn('action="/org/delete"', body)

        csrf = self.driver.csrf("/org")
        status, body, _ = self.driver.post(
            "/org/delete", {"_csrf": csrf, "confirmation": "DELETE"}
        )
        self.assertEqual(status, 403)
        self.assertIn("Only the organization owner can delete it", body)
        self.assertIsNotNone(self.driver.backend.get_tenant(self.tenant_id))


class TestMemberCannotAdmin(unittest.TestCase):
    """§9.2: member (non-admin) cannot invite, remove, or change role."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.owner_email = f"owner{time.time_ns()}@example.com"
        self.owner_password = "owner-password-ok"
        self.driver.login(self.owner_email, self.owner_password)
        self.tenant_id = self.driver.tenant_for_email(self.owner_email)
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
        self.driver.accept_invite(token, self.member_email, self.member_password)
        self.member_acct = self.driver.account_id_for_email(self.tenant_id, self.member_email)
        # Log in as the member.
        self.driver.cookies.clear()
        self.driver.post("/login", {"email": self.member_email, "password": self.member_password})

    def tearDown(self):
        self.driver.close()

    def test_member_cannot_invite(self):
        csrf = self.driver.csrf()
        status, _, _ = self.driver.post(
            "/org/invite",
            {"email": f"x{time.time_ns()}@example.com", "role": "member", "_csrf": csrf},
        )
        self.assertEqual(status, 403)

    def test_member_org_page_hides_admin_controls(self):
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        for action in ("/org/invite", "/org/role", "/org/remove"):
            self.assertNotIn(f'action="{action}"', body)
        self.assertIn("Only organization admins can invite or manage members", body)
        self.assertIn('action="/org/leave"', body)

    def test_member_cannot_remove(self):
        owner_acct = self.driver.account_id_for_email(self.tenant_id, self.owner_email)
        csrf = self.driver.csrf()
        status, _, _ = self.driver.post(
            "/org/remove",
            {"account_id": owner_acct, "_csrf": csrf},
        )
        self.assertEqual(status, 403)

    def test_member_cannot_change_role(self):
        csrf = self.driver.csrf()
        status, _, _ = self.driver.post(
            "/org/role",
            {"account_id": self.member_acct, "role": "admin", "_csrf": csrf},
        )
        self.assertEqual(status, 403)


class TestOneOrgPerAccount(unittest.TestCase):
    """§8 + §9.2: one-org-per-account enforced at POST /org and at invite-accept."""

    def setUp(self):
        self.driver = WebAppDriver()

    def tearDown(self):
        self.driver.close()

    def test_member_cannot_create_second_org(self):
        email = f"u{time.time_ns()}@example.com"
        password = "password-ok"
        self.driver.login(email, password)
        acct = self.driver.account_id_for_email(
            self.driver.tenant_for_email(email), email
        )
        # Attempt to create a second org while already a member. The one-org
        # refusal fires BEFORE CSRF (design §3.2/§9.2 #15).
        status, _, _ = self.driver.post(
            "/org",
            {"email": "second@example.com", "password": "other-password-123", "_csrf": "x"},
        )
        self.assertEqual(status, 400)
        # No second membership.
        self.assertEqual(self.driver.membership_count(acct), 1)

    def test_invite_accept_refused_when_account_already_in_org(self):
        # Org A with owner A.
        owner_a = f"ownera{time.time_ns()}@example.com"
        password_a = "password-a-ok"
        self.driver.login(owner_a, password_a)
        tenant_a = self.driver.tenant_for_email(owner_a)
        acct_a = self.driver.account_id_for_email(tenant_a, owner_a)
        # Org B invites owner A's email. Need an owner B session.
        self.driver.cookies.clear()
        owner_b = f"ownerb{time.time_ns()}@example.com"
        password_b = "password-b-ok"
        self.driver.login(owner_b, password_b)
        # Invite owner A to org B.
        csrf = self.driver.csrf()
        self.driver.post(
            "/org/invite",
            {"email": owner_a, "role": "member", "_csrf": csrf},
        )
        token_b = re.search(
            r"(fiv_[A-Za-z0-9_-]+)", self.driver.last_outbox_body(owner_a)
        ).group(1)
        # Owner A accepts the invite to org B — must be refused (already in org A).
        status, _, _ = self.driver.accept_invite(token_b, owner_a, password_a)
        self.assertEqual(status, 400)
        # Owner A still has exactly one membership (org A).
        self.assertEqual(self.driver.membership_count(acct_a), 1)


class TestCrossTenantVisibility(unittest.TestCase):
    """§9.3: org-A member must not see org-B emails."""

    def setUp(self):
        self.driver = WebAppDriver()

    def tearDown(self):
        self.driver.close()

    def test_cross_tenant_member_list_isolated(self):
        # Org A.
        owner_a = f"ownera{time.time_ns()}@example.com"
        password_a = "password-a-ok"
        self.driver.login(owner_a, password_a)
        # Org B.
        self.driver.cookies.clear()
        owner_b = f"ownerb{time.time_ns()}@example.com"
        password_b = "password-b-ok"
        self.driver.login(owner_b, password_b)
        # Log in as org A owner and check /org.
        self.driver.cookies.clear()
        self.driver.post("/login", {"email": owner_a, "password": password_a})
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        self.assertIn(owner_a, body)
        self.assertNotIn(owner_b, body)


class TestRoleNamesRender(unittest.TestCase):
    """§3.2: the /org page shows role names for each row."""

    def setUp(self):
        self.driver = WebAppDriver()
        self.owner_email = f"owner{time.time_ns()}@example.com"
        self.owner_password = "owner-password-ok"
        self.driver.login(self.owner_email, self.owner_password)

    def tearDown(self):
        self.driver.close()

    def test_role_names_render_on_org_page(self):
        status, body, _ = self.driver.get("/org")
        self.assertEqual(status, 200)
        # The owner's row shows 'owner'.
        self.assertIn("owner", body)
        # The other role names are present in the page (as labels/options).
        self.assertIn("admin", body)
        self.assertIn("member", body)
