"""Wave H — web org/member routes integration contract (RED).

The finalisma_cloud.web package does not exist yet — this file must fail at
import with ModuleNotFoundError.

Drives real HTTP against an in-process FinalismaWebApp server. Route contract
from docs/WEBAPP_DESIGN.md sections 3.1, 3.2, 4, 8, 9.2, 9.3, 10.
"""

from __future__ import annotations

import http.client
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

from finalisma_cloud.storage import SqliteWalBackend
from finalisma_cloud.identity.schema import ensure_schema
from finalisma_cloud.web.app import FinalismaWebApp  # RED: package absent

SITE_DIR = str(ROOT / "site")


class WebAppDriver:
    """In-process HTTP driver for FinalismaWebApp.

    Contract: FinalismaWebApp(backend, static_dir=..., state_dir=...)
    exposes .handler (a BaseHTTPRequestHandler subclass) and is driven on
    ("127.0.0.1", 0) with finally teardown.
    """

    def __init__(self):
        import http.server

        self._tmp = tempfile.TemporaryDirectory()
        self.backend = SqliteWalBackend(str(Path(self._tmp.name) / "cloud.db"))
        self.backend.initialize()
        ensure_schema(self.backend)
        self.app = FinalismaWebApp(
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
        self.post("/signup", {"email": email, "password": password, "_csrf": "x"})
        body = self.last_outbox_body(email)
        token = re.search(r"(fvt_[A-Za-z0-9_-]+)", body).group(1)
        self.post("/verify", {"token": token, "_csrf": "x"})
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
        self.assertIsNotNone(self.driver.extract_csrf(body))
        # POST accept.
        status, _, headers = self.driver.post(
            f"/invite/{token}",
            {"email": invitee_email, "password": invitee_password},
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
        self.driver.post(
            f"/invite/{token}",
            {"email": self.member_email, "password": self.member_password},
        )
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
        self.driver.post(
            f"/invite/{token}",
            {"email": self.member_email, "password": self.member_password},
        )
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
        self.driver.post(
            f"/invite/{token}",
            {"email": self.member_email, "password": self.member_password},
        )
        # Re-login as owner.
        self.driver.cookies.clear()
        self.driver.post("/login", {"email": self.owner_email, "password": self.owner_password})

    def tearDown(self):
        self.driver.close()

    def test_owner_leave_refused_while_other_members_exist(self):
        csrf = self.driver.csrf()
        status, _, _ = self.driver.post("/org/leave", {"_csrf": csrf})
        self.assertIn(status, (400, 403))
        # Org still exists.
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
        self.driver.post(
            f"/invite/{token}",
            {"email": self.member_email, "password": self.member_password},
        )
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
        status, _, _ = self.driver.post(
            f"/invite/{token_b}",
            {"email": owner_a, "password": password_a},
        )
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
