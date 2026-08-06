"""Wave H — web auth routes integration contract (RED).

Drives real HTTP against an in-process server. The finalisma_cloud.web package
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
        # Pre-create a verified account directly via identity plane.
        from finalisma_cloud.identity.accounts import _create_account

        self.driver.backend.create_tenant("tenant-x", self.email, "free")
        _create_account(self.driver.backend, "tenant-x", self.email, self.password, email_verified=1)

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

    def test_verify_get_auto_verifies(self):
        status, _, headers = self.driver.get(f"/verify?token={self.raw_token}")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/login?verified=1"))

    def test_verify_garbage_token_renders_error(self):
        status, body, _ = self.driver.get("/verify?token=not_a_real_token")
        self.assertEqual(status, 400)


class TestResetPassword(unittest.TestCase):
    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"reset{time.time_ns()}@example.com"
        self.old_password = "old-password-ok-long"
        self.driver.backend.create_tenant("tenant-r", self.email, "free")
        from finalisma_cloud.identity.accounts import _create_account

        _create_account(
            self.driver.backend, "tenant-r", self.email, self.old_password, email_verified=1
        )

    def tearDown(self):
        self.driver.close()

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
        from finalisma_cloud.identity.accounts import _create_account

        _create_account(
            self.driver.backend, "tenant-l", self.email, self.password, email_verified=1
        )
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


class TestNoSecretsInHtml(unittest.TestCase):
    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"secrets{time.time_ns()}@example.com"
        self.password = "secret-password-do-not-leak"
        self.driver.backend.create_tenant("tenant-s", self.email, "free")
        from finalisma_cloud.identity.accounts import _create_account

        _create_account(
            self.driver.backend, "tenant-s", self.email, self.password, email_verified=1
        )

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
