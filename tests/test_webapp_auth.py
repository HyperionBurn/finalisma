"""Wave H — web auth routes integration contract (RED).

Drives real HTTP against an in-process server. The weft_cloud.web package
does not exist yet — this file must fail at import with ModuleNotFoundError.
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

from weft_cloud.storage import SqliteWalBackend
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.web.app import WeftWebApp  # RED: package absent

SITE_DIR = str(ROOT / "site")


class WebAppDriver:
    """In-process HTTP driver for WeftWebApp.

    Contract: WeftWebApp(backend, static_dir=..., state_dir=...)
    exposes .handler (a BaseHTTPRequestHandler subclass) and is driven on
    ("127.0.0.1", 0) with finally teardown.
    """

    def __init__(self, smtp_configured: bool | None = None, auth_rate_limits=None):
        import http.server

        self._tmp = tempfile.TemporaryDirectory()
        self.backend = SqliteWalBackend(str(Path(self._tmp.name) / "cloud.db"))
        self.backend.initialize()
        ensure_schema(self.backend)
        self.app = WeftWebApp(
            self.backend,
            static_dir=SITE_DIR,
            state_dir=str(Path(self._tmp.name) / "state"),
            smtp_configured=smtp_configured,
            auth_rate_limits=auth_rate_limits,
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

    def last_outbox_body(self, to_email):
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (to_email,),
            ).fetchone()
        return row["body"] if row else None

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        finally:
            self.backend.close()
            self._tmp.cleanup()


def provision_verified_member(driver: WebAppDriver, tenant_id: str, email: str, password: str) -> str:
    """Provision a web-auth fixture with the production membership contract."""
    from weft_cloud.identity.accounts import _create_account
    from weft_cloud.storage import utc_now_iso

    account_id = _create_account(
        driver.backend, tenant_id, email, password, email_verified=1
    )
    with driver.backend.transaction() as tx:
        tx.execute(
            "INSERT OR IGNORE INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
            "VALUES (?, ?, 'owner', ?)",
            (tenant_id, account_id, utc_now_iso()),
        )
        tx.commit()
    return account_id


class TestSignup(unittest.TestCase):
    def setUp(self):
        self.driver = WebAppDriver()

    def tearDown(self):
        self.driver.close()

    def test_signup_get_renders_form_with_csrf(self):
        status, body, _ = self.driver.get("/signup")
        self.assertEqual(status, 200)
        self.assertIn('name="email"', body)
        self.assertIn('name="password"', body)
        self.assertIsNotNone(self.driver.extract_csrf(body))

    def test_signup_post_creates_account_and_redirects_no_session(self):
        email = f"u{time.time_ns()}@example.com"
        password = "correct-horse-battery-staple"
        status, body, headers = self.driver.post(
            "/signup", {"email": email, "password": password, "_csrf": "irrelevant-for-pub-route"}
        )
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/login?verify_sent=1"))
        self.assertNotIn("fss_session", self.driver.cookies)
        outbox_body = self.driver.last_outbox_body(email)
        self.assertIsNotNone(outbox_body)
        m = re.search(r"(fvt_[A-Za-z0-9_\-]+)", outbox_body)
        self.assertIsNotNone(m, "verification fvt_ token must appear in outbox body")

    def test_signup_short_password_rejected(self):
        email = f"short{time.time_ns()}@example.com"
        status, body, _ = self.driver.post(
            "/signup", {"email": email, "password": "abc", "_csrf": "x"}
        )
        self.assertEqual(status, 400)
        # No account created → outbox empty for this email.
        self.assertIsNone(self.driver.last_outbox_body(email))

    def test_signup_duplicate_email_rejected(self):
        email = f"dup{time.time_ns()}@example.com"
        password = "first-password-ok"
        status1, _, _ = self.driver.post(
            "/signup", {"email": email, "password": password, "_csrf": "x"}
        )
        self.assertEqual(status1, 303)
        status2, body2, _ = self.driver.post(
            "/signup", {"email": email, "password": "second-password-ok-too", "_csrf": "x"}
        )
        self.assertEqual(status2, 400)


class TestLogin(unittest.TestCase):
    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"login{time.time_ns()}@example.com"
        self.password = "login-password-ok"
        self.driver.backend.create_tenant("tenant-x", self.email, "free")
        provision_verified_member(self.driver, "tenant-x", self.email, self.password)

    def tearDown(self):
        self.driver.close()

    def test_login_get_renders_form(self):
        status, body, _ = self.driver.get("/login")
        self.assertEqual(status, 200)
        self.assertIn('name="email"', body)
        self.assertIn('name="password"', body)
        self.assertIsNotNone(self.driver.extract_csrf(body))

    def test_login_correct_sets_session_cookie(self):
        status, _, headers = self.driver.post(
            "/login", {"email": self.email, "password": self.password}
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertIn("fss_session", self.driver.cookies)

    def test_login_wrong_password_renders_error_no_cookie(self):
        status, body, _ = self.driver.post(
            "/login", {"email": self.email, "password": "wrong-password-xyz"}
        )
        self.assertEqual(status, 200)
        self.assertNotIn("fss_session", self.driver.cookies)
        self.assertNotIn("wrong-password-xyz", body)

    def test_login_unknown_email_non_committal(self):
        status, body, _ = self.driver.post(
            "/login", {"email": "nobody@example.com", "password": "whatever-password-123"}
        )
        self.assertEqual(status, 200)
        self.assertNotIn("fss_session", self.driver.cookies)
        self.assertNotIn("whatever-password-123", body)
        # Non-committal: must not reveal whether the email exists.
        self.assertNotIn("not registered", body.lower())
        self.assertNotIn("no account", body.lower())
        self.assertNotIn("unknown email", body.lower())

    def test_session_fixation_new_cookie_on_login(self):
        # Seed a bogus pre-login session cookie.
        self.driver.cookies["fss_session"] = "fss_bogus_pre_login_value"
        status, _, headers = self.driver.post(
            "/login", {"email": self.email, "password": self.password}
        )
        self.assertEqual(status, 303)
        self.assertIn("fss_session", self.driver.cookies)
        self.assertNotEqual(self.driver.cookies["fss_session"], "fss_bogus_pre_login_value")
        # Old cookie must no longer work.
        old_cookie = "fss_bogus_pre_login_value"
        saved = self.driver.cookies.copy()
        self.driver.cookies = {"fss_session": old_cookie}
        status2, _, headers2 = self.driver.get("/")
        self.assertEqual(status2, 303)
        self.assertEqual(headers2["Location"], "/login")
        self.driver.cookies = saved

    def test_refresh_rotates_browser_cookie_with_csrf(self):
        status, _, _ = self.driver.post(
            "/login", {"email": self.email, "password": self.password}
        )
        self.assertEqual(status, 303)
        old_cookie = self.driver.cookies["fss_session"]
        status, body, _ = self.driver.get("/")
        self.assertEqual(status, 200)
        csrf = self.driver.extract_csrf(body)
        self.assertIsNotNone(csrf)

        status, _, headers = self.driver.post("/refresh", {"_csrf": csrf})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        new_cookie = self.driver.cookies["fss_session"]
        self.assertNotEqual(new_cookie, old_cookie)

        self.driver.cookies = {"fss_session": old_cookie}
        old_status, _, old_headers = self.driver.get("/")
        self.assertEqual(old_status, 303)
        self.assertEqual(old_headers["Location"], "/login")
        self.driver.cookies = {"fss_session": new_cookie}
        new_status, _, _ = self.driver.get("/")
        self.assertEqual(new_status, 200)

    def test_refresh_wrong_csrf_preserves_browser_session(self):
        status, _, _ = self.driver.post(
            "/login", {"email": self.email, "password": self.password}
        )
        self.assertEqual(status, 303)
        old_cookie = self.driver.cookies["fss_session"]
        status, _, _ = self.driver.post("/refresh", {"_csrf": "wrong"})
        self.assertEqual(status, 403)
        self.assertEqual(self.driver.cookies["fss_session"], old_cookie)
        status, _, _ = self.driver.get("/")
        self.assertEqual(status, 200)


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"verify{time.time_ns()}@example.com"
        self.password = "verify-password-ok"
        # Fresh signup writes the verification email to the outbox.
        self.driver.post("/signup", {"email": self.email, "password": self.password, "_csrf": "x"})
        outbox_body = self.driver.last_outbox_body(self.email)
        self.assertIsNotNone(outbox_body, "signup must write a verification email")
        m = re.search(r"(fvt_[A-Za-z0-9_\-]+)", outbox_body)
        self.raw_token = m.group(1)

    def tearDown(self):
        self.driver.close()

    def test_verify_post_valid_token_redirects(self):
        status, _, headers = self.driver.post("/verify", {"token": self.raw_token})
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/login?verified=1"))

    def test_verify_token_single_use(self):
        status1, _, _ = self.driver.post("/verify", {"token": self.raw_token})
        self.assertEqual(status1, 303)
        status2, body2, _ = self.driver.post("/verify", {"token": self.raw_token})
        self.assertEqual(status2, 400)

    def test_verify_get_renders_confirm_page_but_does_not_verify(self):
        # A GET must never mutate state: link scanners, antivirus and mail-client
        # prefetchers fetch GET URLs automatically, which would verify the address
        # without a human ever clicking. GET only renders a confirmation page;
        # the POST confirm (the human click) is the actual verification step.
        status, body, headers = self.driver.get(f"/verify?token={self.raw_token}")
        self.assertEqual(status, 200)
        self.assertNotIn("Location", headers)  # no redirect, no mutation
        self.assertIn("Confirm your email", body)
        self.assertIn('action="/verify"', body)  # POST confirm form
        self.assertIn(self.raw_token, body)  # the confirm form carries the token
        # The account is STILL unverified after the GET.
        with self.driver.backend.transaction() as tx:
            row = tx.execute(
                "SELECT email_verified FROM cloud_identity_accounts WHERE email = ?",
                (self.email,),
            ).fetchone()
        self.assertEqual(row["email_verified"], 0)
        # The POST confirm — the human click — is what actually verifies.
        status2, _, headers2 = self.driver.post("/verify", {"token": self.raw_token})
        self.assertEqual(status2, 303)
        self.assertTrue(headers2["Location"].startswith("/login?verified=1"))

    def test_verify_get_with_garbage_token_renders_confirm_page(self):
        # GET never validates the token: validating without consuming would be a
        # token-validity oracle, and consuming on a GET is the mutation we just
        # removed. Garbage tokens render the same confirm page; the 400 error is
        # surfaced only at the POST confirm step.
        status, body, _ = self.driver.get("/verify?token=not_a_real_token")
        self.assertEqual(status, 200)
        self.assertIn("Confirm your email", body)
        # POST with the garbage token is refused.
        status2, _, _ = self.driver.post("/verify", {"token": "not_a_real_token"})
        self.assertEqual(status2, 400)


class TestResetPassword(unittest.TestCase):
    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"reset{time.time_ns()}@example.com"
        self.old_password = "old-password-ok-long"
        self.driver.backend.create_tenant("tenant-r", self.email, "free")
        provision_verified_member(self.driver, "tenant-r", self.email, self.old_password)

    def tearDown(self):
        self.driver.close()

    def test_reset_request_get_renders_form_with_csrf(self):
        """GET /reset-request must render the request form (email + CSRF),
        posting to the existing POST handler — the login page links here, so
        a user who forgets their password needs a working entry point.
        """
        status, body, _ = self.driver.get("/reset-request")
        self.assertEqual(status, 200)
        self.assertIn('name="email"', body)
        self.assertIsNotNone(self.driver.extract_csrf(body))
        self.assertIn('action="/reset-request"', body)
        self.assertIn('type="email"', body)

    def test_reset_request_click_through_posts_and_writes_outbox(self):
        """End-to-end click-through: GET the form, submit it, and confirm the
        POST is accepted and a reset row lands in the outbox.
        """
        status, body, _ = self.driver.get("/reset-request")
        self.assertEqual(status, 200)
        csrf = self.driver.extract_csrf(body)
        self.assertIsNotNone(csrf)
        post_status, _, headers = self.driver.post(
            "/reset-request", {"email": self.email, "_csrf": csrf}
        )
        self.assertEqual(post_status, 303)
        self.assertTrue(headers["Location"].startswith("/login?reset_sent=1"))
        outbox_body = self.driver.last_outbox_body(self.email)
        self.assertIsNotNone(outbox_body, "reset request must write an outbox row")
        m = re.search(r"(frt_[A-Za-z0-9_\-]+)", outbox_body)
        self.assertIsNotNone(m, "reset token must appear in outbox body")

    def test_reset_request_always_redirects_no_enumeration(self):
        status_known, _, headers_known = self.driver.post(
            "/reset-request", {"email": self.email}
        )
        status_unknown, _, headers_unknown = self.driver.post(
            "/reset-request", {"email": "ghost@example.com"}
        )
        self.assertEqual(status_known, 303)
        self.assertEqual(status_unknown, 303)
        self.assertTrue(headers_known["Location"].startswith("/login?reset_sent=1"))
        self.assertTrue(headers_unknown["Location"].startswith("/login?reset_sent=1"))

    def test_reset_flow_then_old_password_fails(self):
        self.driver.post("/reset-request", {"email": self.email})
        outbox_body = self.driver.last_outbox_body(self.email)
        m = re.search(r"(frt_[A-Za-z0-9_\-]+)", outbox_body)
        raw_token = m.group(1)

        # GET /reset renders form with password + _csrf + hidden token.
        status, body, _ = self.driver.get(f"/reset?token={raw_token}")
        self.assertEqual(status, 200)
        self.assertIn('name="password"', body)
        self.assertIsNotNone(self.driver.extract_csrf(body))
        self.assertIn(f'value="{raw_token}"', body)

        new_password = "brand-new-password-ok-123"
        csrf = self.driver.extract_csrf(body)
        status2, _, headers2 = self.driver.post(
            "/reset", {"token": raw_token, "password": new_password, "_csrf": csrf}
        )
        self.assertEqual(status2, 303)
        self.assertTrue(headers2["Location"].startswith("/login?reset_done=1"))

        # Old password fails.
        s_old, _, _ = self.driver.post(
            "/login", {"email": self.email, "password": self.old_password}
        )
        self.assertEqual(s_old, 200)
        # New password works.
        s_new, _, h_new = self.driver.post(
            "/login", {"email": self.email, "password": new_password}
        )
        self.assertEqual(s_new, 303)
        self.assertEqual(h_new["Location"], "/")

        # Token reuse rejected.
        s_reuse, _, _ = self.driver.post(
            "/reset", {"token": raw_token, "password": "yet-another-pass-123", "_csrf": csrf}
        )
        self.assertEqual(s_reuse, 400)

    def test_reset_kills_sessions(self):
        # Log in, hold the session cookie.
        s, _, h = self.driver.post(
            "/login", {"email": self.email, "password": self.old_password}
        )
        self.assertEqual(s, 303)
        self.assertIn("fss_session", self.driver.cookies)
        pre_reset_cookie = self.driver.cookies["fss_session"]

        # Trigger reset.
        self.driver.post("/reset-request", {"email": self.email})
        outbox_body = self.driver.last_outbox_body(self.email)
        m = re.search(r"(frt_[A-Za-z0-9_\-]+)", outbox_body)
        raw_token = m.group(1)
        _, form_body, _ = self.driver.get(f"/reset?token={raw_token}")
        csrf = self.driver.extract_csrf(form_body)
        self.driver.post(
            "/reset", {"token": raw_token, "password": "killed-sessions-pass-123", "_csrf": csrf}
        )

        # Pre-reset cookie must no longer work.
        saved = self.driver.cookies.copy()
        self.driver.cookies = {"fss_session": pre_reset_cookie}
        status, _, headers = self.driver.get("/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login")
        self.driver.cookies = saved


class TestLogout(unittest.TestCase):
    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"logout{time.time_ns()}@example.com"
        self.password = "logout-password-ok"
        self.driver.backend.create_tenant("tenant-l", self.email, "free")
        provision_verified_member(self.driver, "tenant-l", self.email, self.password)
        s, _, _ = self.driver.post("/login", {"email": self.email, "password": self.password})
        self.assertEqual(s, 303)
        self.assertIn("fss_session", self.driver.cookies)

    def tearDown(self):
        self.driver.close()

    def test_logout_clears_session_and_redirects(self):
        # We need a CSRF token. The dashboard home renders the logout form.
        _, body, _ = self.driver.get("/")
        csrf = self.driver.extract_csrf(body)
        self.assertIsNotNone(csrf)
        saved_cookie = self.driver.cookies.get("fss_session")
        status, _, headers = self.driver.post("/logout", {"_csrf": csrf})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login")
        self.assertNotIn("fss_session", self.driver.cookies)
        # After logout, GET / redirects.
        status2, _, headers2 = self.driver.get("/")
        self.assertEqual(status2, 303)
        self.assertEqual(headers2["Location"], "/login")
        _ = saved_cookie  # referenced for clarity

    def test_logout_missing_csrf_403_and_preserves_session(self):
        saved_cookie = self.driver.cookies.get("fss_session")
        self.assertIsNotNone(saved_cookie)
        status, _, _ = self.driver.post("/logout", {})
        self.assertEqual(status, 403)
        # Session cookie must NOT be cleared on CSRF failure.
        self.assertIn("fss_session", self.driver.cookies)
        self.assertEqual(self.driver.cookies["fss_session"], saved_cookie)

    def test_logout_wrong_csrf_403_and_preserves_session(self):
        saved_cookie = self.driver.cookies.get("fss_session")
        status, _, _ = self.driver.post("/logout", {"_csrf": "definitely-wrong-token"})
        self.assertEqual(status, 403)
        self.assertIn("fss_session", self.driver.cookies)
        self.assertEqual(self.driver.cookies["fss_session"], saved_cookie)


class TestGatedRoutes(unittest.TestCase):
    def setUp(self):
        self.driver = WebAppDriver()

    def tearDown(self):
        self.driver.close()

    def test_unauthenticated_gated_routes_redirect_to_login(self):
        for path in ("/", "/org", "/rooms", "/room/nope"):
            with self.subTest(path=path):
                status, body, headers = self.driver.get(path)
                self.assertEqual(status, 303, f"{path} should redirect")
                self.assertEqual(headers["Location"], "/login")
                # No dashboard content leaked.
                self.assertNotIn("Rooms", body)


class TestEmailMessaging(unittest.TestCase):
    """JOB 2 — signup/reset messaging must be honest about whether mail can
    actually be sent.

    The login page renders a flash notice when it lands with
    ``verify_sent``/``reset_sent`` query params. When SMTP is configured the
    copy says "check your email". When it is NOT configured the copy says
    plainly that email delivery is not enabled and what to do instead. The
    branch is driven by the app's SMTP config, not a constant — assert both
    branches. No reset token or link may ever appear in any response body or
    page.
    """

    def _signup(self, driver: WebAppDriver) -> str:
        email = f"msg{time.time_ns()}@example.com"
        status, _, headers = driver.post(
            "/signup", {"email": email, "password": "correct-horse-battery-staple", "_csrf": "x"}
        )
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/login?verify_sent=1"))
        return email

    def test_signup_messaging_unconfigured_says_email_disabled(self):
        driver = WebAppDriver(smtp_configured=False)
        try:
            self._signup(driver)
            status, body, _ = driver.get("/login?verify_sent=1")
            self.assertEqual(status, 200)
            self.assertIn("Email delivery is not enabled", body)
            self.assertIn("log in now without verifying", body)
            self.assertNotIn("Check your email", body)
        finally:
            driver.close()

    def test_signup_messaging_configured_says_check_your_email(self):
        driver = WebAppDriver(smtp_configured=True)
        try:
            self._signup(driver)
            status, body, _ = driver.get("/login?verify_sent=1")
            self.assertEqual(status, 200)
            self.assertIn("Check your email", body)
            self.assertNotIn("Email delivery is not enabled", body)
        finally:
            driver.close()

    def test_reset_request_messaging_unconfigured_says_contact_operator(self):
        driver = WebAppDriver(smtp_configured=False)
        try:
            email = f"resetmsg{time.time_ns()}@example.com"
            driver.backend.create_tenant("tenant-rm", email, "free")
            provision_verified_member(driver, "tenant-rm", email, "old-password-ok")
            status, _, headers = driver.post("/reset-request", {"email": email})
            self.assertEqual(status, 303)
            self.assertTrue(headers["Location"].startswith("/login?reset_sent=1"))
            status, body, _ = driver.get("/login?reset_sent=1")
            self.assertEqual(status, 200)
            self.assertIn("Email delivery is not enabled", body)
            self.assertIn("Contact the operator", body)
            self.assertNotIn("Check your email", body)
        finally:
            driver.close()

    def test_reset_request_messaging_configured_says_check_your_email(self):
        driver = WebAppDriver(smtp_configured=True)
        try:
            email = f"resetcfg{time.time_ns()}@example.com"
            driver.backend.create_tenant("tenant-rc", email, "free")
            provision_verified_member(driver, "tenant-rc", email, "old-password-ok")
            driver.post("/reset-request", {"email": email})
            status, body, _ = driver.get("/login?reset_sent=1")
            self.assertEqual(status, 200)
            self.assertIn("Check your email", body)
            self.assertNotIn("Email delivery is not enabled", body)
        finally:
            driver.close()

    def test_no_reset_token_or_link_in_any_response_body_or_page(self):
        """The reset-request flow must never leak a reset token or reset link.

        We read the raw token out of the outbox row (the only place it is
        written), then assert it appears in NO HTTP response body or page —
        neither the 303 response nor the login page it redirects to.
        """
        driver = WebAppDriver(smtp_configured=False)
        try:
            email = f"leakcheck{time.time_ns()}@example.com"
            driver.backend.create_tenant("tenant-lk", email, "free")
            provision_verified_member(driver, "tenant-lk", email, "old-password-ok")
            status, body, headers = driver.post("/reset-request", {"email": email})
            self.assertEqual(status, 303)
            # 303 has no body, but assert the invariant anyway.
            self.assertNotIn("frt_", body)
            outbox_body = driver.last_outbox_body(email)
            self.assertIsNotNone(outbox_body)
            m = re.search(r"(frt_[A-Za-z0-9_\-]+)", outbox_body)
            self.assertIsNotNone(m, "reset token must exist in outbox")
            raw_token = m.group(1)

            # Every HTTP surface reachable in this flow: the 303 body, the
            # login page after redirect, and the login page with no params.
            surfaces = [
                body,
                driver.get("/login?reset_sent=1")[1],
                driver.get("/login")[1],
            ]
            for surface in surfaces:
                self.assertNotIn(raw_token, surface)
                self.assertNotIn("/reset?token=", surface)
        finally:
            driver.close()


class TestResetRequestRateLimits(unittest.TestCase):
    """POST /reset-request — mail-bomb protection via the shared rate_limit.py.

    The limit is keyed on the email REGARDLESS of whether the account exists,
    so a throttled unknown address returns the identical 429 as a throttled
    known address — the refusal is not an existence oracle (mirroring the
    existing always-redirects enumeration invariant, which survives: under the
    limit both known and unknown addresses still 303 to reset_sent).
    """

    _LIMITS = {"reset_request": {"ip": 10, "email": 3, "window_seconds": 2}}

    def setUp(self):
        self.driver = WebAppDriver(auth_rate_limits=self._LIMITS)

    def tearDown(self):
        self.driver.close()

    def _post(self, email: str):
        return self.driver.post("/reset-request", {"email": email})

    def test_limit_triggers_after_n_requests(self):
        for _ in range(3):
            status, _, headers = self._post("target@example.com")
            self.assertEqual(status, 303, "first 3 requests must be allowed")
            self.assertTrue(headers["Location"].startswith("/login?reset_sent=1"))
        status, _, headers = self._post("target@example.com")
        self.assertEqual(status, 429, "4th request to the same address must be refused")
        self.assertEqual(headers.get("Retry-After"), "2")

    def test_throttled_known_equals_throttled_unknown(self):
        # A known address (registered) and a ghost address must be refused
        # with an IDENTICAL 429 so throttling cannot enumerate accounts.
        self.driver.backend.create_tenant("t-reset", "known@example.com", "free")
        provision_verified_member(self.driver, "t-reset", "known@example.com", "CorrectHorse!1")
        for _ in range(3):
            self._post("known@example.com")
        known = self._post("known@example.com")
        for _ in range(3):
            self._post("ghost@example.org")
        ghost = self._post("ghost@example.org")

        self.assertEqual(known[0], 429)
        self.assertEqual(ghost[0], 429)
        self.assertEqual(known[1], ghost[1],
                         "throttled-known and throttled-unknown bodies differ — oracle")
        self.assertEqual(known[2].get("Retry-After"), ghost[2].get("Retry-After"))

    def test_limit_resets_after_window(self):
        for _ in range(3):
            self._post("reset-me@example.com")
        status, _, _ = self._post("reset-me@example.com")
        self.assertEqual(status, 429)
        time.sleep(2.4)
        status, _, headers = self._post("reset-me@example.com")
        self.assertEqual(status, 303,
                         "after the window the limit must reset and allow again")
        self.assertTrue(headers["Location"].startswith("/login?reset_sent=1"))

    def test_normal_usage_unaffected(self):
        # A person asking for a reset once, then again after a mistake, works.
        status, _, _ = self._post("person@example.com")
        self.assertEqual(status, 303)
        status, _, _ = self._post("person@example.com")
        self.assertEqual(status, 303)


class TestNoSecretsInHtml(unittest.TestCase):
    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"secrets{time.time_ns()}@example.com"
        self.password = "secret-password-do-not-leak"
        self.driver.backend.create_tenant("tenant-s", self.email, "free")
        provision_verified_member(self.driver, "tenant-s", self.email, self.password)

    def tearDown(self):
        self.driver.close()

    def test_login_page_does_not_contain_password(self):
        _, body, _ = self.driver.get("/login")
        self.assertNotIn(self.password, body)

    def test_wrong_password_error_does_not_leak_password(self):
        _, body, _ = self.driver.post(
            "/login", {"email": self.email, "password": self.password}
        )
        # This is the success path (verified account), but even the success
        # path must not render the password back.
        self.assertNotIn(self.password, body)

    def test_signup_page_does_not_contain_submitted_password(self):
        _, body, _ = self.driver.post(
            "/signup",
            {"email": self.email, "password": self.password, "_csrf": "x"},
        )
        self.assertNotIn(self.password, body)


if __name__ == "__main__":
    unittest.main()
