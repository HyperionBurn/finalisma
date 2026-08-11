"""Security response headers + session cookie attributes (RED).

The live service shipped no HSTS/CSP/frame-protection/Referrer-Policy/
Permissions-Policy and its session cookie omitted ``Secure`` (external
audit, confirmed). These tests pin the FIX: every browser-facing HTML
response carries the security header block, the session cookie carries
``Secure`` exactly when the request is https (TLS, or ``X-Forwarded-Proto``
from the TLS-terminating proxy) or when the operator forces it via the
``WEFT_WEB_SECURE_COOKIES`` env flag — and NOT over a plain-http dev
connection, so a ``Secure`` cookie never silently breaks local development.

Drives real HTTP against an in-process ``WeftWebApp`` server.
"""

from __future__ import annotations

import http.client
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.storage import SqliteWalBackend  # noqa: E402
from weft_cloud.identity.schema import ensure_schema  # noqa: E402
from weft_cloud.web.app import WeftWebApp  # noqa: E402

SITE_DIR = str(ROOT / "site")

REQUIRED_ALWAYS_HEADERS = (
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "X-Frame-Options",
    "Permissions-Policy",
)


class WebAppDriver:
    def __init__(self):
        import http.server

        self._tmp = tempfile.TemporaryDirectory()
        self.backend = SqliteWalBackend(str(Path(self._tmp.name) / "cloud.db"))
        self.backend.initialize()
        ensure_schema(self.backend)
        self.app = WeftWebApp(
            self.backend, static_dir=SITE_DIR, state_dir=str(Path(self._tmp.name) / "state")
        )
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.app.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.cookies: dict[str, str] = {}

    def _base_headers(self, extra_headers: dict[str, str] | None) -> dict[str, str]:
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        headers = {"Cookie": cookie} if cookie else {}
        if extra_headers:
            headers.update(extra_headers)
        return headers

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

    def get(self, path, extra_headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        conn.request("GET", path, headers=self._base_headers(extra_headers))
        resp = conn.getresponse()
        self._collect_cookies(resp)
        body = resp.read().decode("utf-8", errors="replace")
        out = dict(resp.getheaders())
        conn.close()
        return resp.status, body, out

    def post(self, path, form: dict, extra_headers=None):
        body = urlencode(form)
        headers = self._base_headers(extra_headers)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        self._collect_cookies(resp)
        resp_body = resp.read().decode("utf-8", errors="replace")
        out = dict(resp.getheaders())
        conn.close()
        return resp.status, resp_body, out

    def last_outbox_body(self, to_email):
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (to_email,),
            ).fetchone()
        return row["body"] if row else None

    def signup_verify_login(self, email, password, extra_headers=None):
        self.post("/signup", {"email": email, "password": password, "_csrf": "x"},
                  extra_headers=extra_headers)
        body = self.last_outbox_body(email)
        vt = re.search(r"(fvt_[A-Za-z0-9_-]+)", body).group(1)
        self.post("/verify", {"token": vt, "_csrf": "x"}, extra_headers=extra_headers)
        status, _, _ = self.post("/login", {"email": email, "password": password},
                                 extra_headers=extra_headers)
        return status == 303 and "fss_session" in self.cookies

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        finally:
            self.backend.close()
            self._tmp.cleanup()


class TestSessionCookieSecureFlag(unittest.TestCase):
    """The session cookie must be Secure exactly when the channel is https."""

    def setUp(self):
        self.d = WebAppDriver()
        self.email = "cookie@example.com"
        self.password = "Password123!"

    def tearDown(self):
        self.d.close()

    def _seed_account(self):
        self.d.post("/signup", {"email": self.email, "password": self.password, "_csrf": "x"})

    def _login_cookie(self, extra_headers=None) -> str:
        self._seed_account()
        status, _, hdrs = self.d.post(
            "/login", {"email": self.email, "password": self.password},
            extra_headers=extra_headers,
        )
        self.assertEqual(status, 303, "login must succeed")
        sc = hdrs.get("Set-Cookie", "")
        self.assertTrue(sc.startswith("fss_session="), f"session cookie missing: {sc!r}")
        return sc

    def test_https_request_sets_secure_httponly_samesite_cookie(self):
        sc = self._login_cookie(extra_headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", sc)
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Lax", sc)
        self.assertIn("Path=/", sc)

    def test_plain_http_request_omits_secure(self):
        sc = self._login_cookie()
        self.assertNotIn("Secure", sc, "Secure cookie on plain http breaks local dev")
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Lax", sc)

    def test_env_flag_forces_secure_over_plain_http(self):
        self.d.close()
        with mock.patch.dict(os.environ, {"WEFT_WEB_SECURE_COOKIES": "1"}):
            self.d = WebAppDriver()
        try:
            sc = self._login_cookie()
            self.assertIn("Secure", sc)
        finally:
            self.d.close()

    def test_env_flag_disables_secure_even_with_forwarded_https(self):
        self.d.close()
        with mock.patch.dict(os.environ, {"WEFT_WEB_SECURE_COOKIES": "0"}):
            self.d = WebAppDriver()
        try:
            sc = self._login_cookie(extra_headers={"X-Forwarded-Proto": "https"})
            self.assertNotIn("Secure", sc)
        finally:
            self.d.close()

    def test_logout_clear_cookie_carries_same_attributes(self):
        self.assertTrue(self.d.signup_verify_login("c@example.com", "Password123!"))
        _, body, _ = self.d.get("/")
        csrf = re.search(r'name="_csrf"\s+value="([^"]+)"', body).group(1)
        status, _, hdrs = self.d.post(
            "/logout", {"_csrf": csrf},
            extra_headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 303)
        sc = hdrs.get("Set-Cookie", "")
        self.assertTrue(sc.startswith("fss_session="), f"clear cookie missing: {sc!r}")
        self.assertIn("Max-Age=0", sc)
        self.assertIn("Secure", sc)
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Lax", sc)


class TestHtmlResponseSecurityHeaders(unittest.TestCase):
    """Every HTML response carries the full security header block."""

    def setUp(self):
        self.d = WebAppDriver()

    def tearDown(self):
        self.d.close()

    def _assert_full_block(self, hdrs: dict, label: str) -> None:
        for name in REQUIRED_ALWAYS_HEADERS:
            self.assertIn(name, hdrs, f"{label}: missing {name}")
        self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(hdrs.get("Referrer-Policy"), "strict-origin-when-cross-origin")
        self.assertEqual(hdrs.get("X-Frame-Options"), "DENY")
        self.assertTrue(
            hdrs.get("Strict-Transport-Security", "").startswith("max-age="),
            f"{label}: HSTS must set a max-age",
        )
        csp = hdrs.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp, f"{label}: frame protection in CSP")

    def test_login_page_html_carries_full_security_block(self):
        status, _, hdrs = self.d.get("/login")
        self.assertEqual(status, 200)
        self._assert_full_block(hdrs, "/login")

    def test_signup_page_html_carries_full_security_block(self):
        status, _, hdrs = self.d.get("/signup")
        self.assertEqual(status, 200)
        self._assert_full_block(hdrs, "/signup")

    def test_static_marketing_html_carries_full_security_block(self):
        status, _, hdrs = self.d.get("/terms.html")
        self.assertEqual(status, 200)
        self._assert_full_block(hdrs, "/terms.html")
        csp = hdrs.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self' 'unsafe-inline'", csp,
                      "static marketing pages need inline-script allowance")

    def test_marketing_index_is_auth_gated_on_web_app_with_headers(self):
        # The web app only serves /terms.html and /privacy.html pre-auth; the
        # marketing index is behind the auth gate (the marketing bundle is
        # served by the site server / nginx instead). The 303 must still carry
        # the always-on header block so the login redirect is protected.
        status, _, hdrs = self.d.get("/index.html")
        self.assertEqual(status, 303)
        self.assertTrue(hdrs.get("Location", "").startswith("/login"))
        for name in REQUIRED_ALWAYS_HEADERS:
            self.assertIn(name, hdrs, f"/index.html redirect: missing {name}")

    def test_unauthenticated_redirect_carries_always_on_headers(self):
        status, _, hdrs = self.d.get("/")
        self.assertEqual(status, 303)
        self.assertTrue(hdrs.get("Location", "").startswith("/login"))
        for name in REQUIRED_ALWAYS_HEADERS:
            self.assertIn(name, hdrs, f"redirect: missing {name}")
        self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff")

    def test_static_assets_after_login_carry_always_on_headers(self):
        self.assertTrue(self.d.signup_verify_login("asset@example.com", "Password123!"))
        status, _, hdrs = self.d.get("/styles.css")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("Strict-Transport-Security", hdrs)
        self.assertIn("Referrer-Policy", hdrs)


class TestLoginStillWorks(unittest.TestCase):
    """End-to-end login must survive the cookie/header change on both channels."""

    def setUp(self):
        self.d = WebAppDriver()

    def tearDown(self):
        self.d.close()

    def test_login_roundtrip_over_plain_http(self):
        self.assertTrue(self.d.signup_verify_login("dev@example.com", "Password123!"))
        status, body, _ = self.d.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Dashboard", body)
        self.assertNotIn(self.d.cookies["fss_session"], body)

    def test_login_roundtrip_over_simulated_https(self):
        hdr = {"X-Forwarded-Proto": "https"}
        # Signup -> verify -> login, all over a simulated-TLS channel. The
        # session cookie is collected WITH the Secure flag, then must still be
        # accepted back on the authenticated dashboard fetch — otherwise a
        # Secure cookie breaks the session behind a TLS-terminating proxy,
        # which is exactly the live deployment shape.
        self.assertTrue(
            self.d.signup_verify_login("prod@example.com", "Password123!", extra_headers=hdr)
        )
        self.assertIn("fss_session", self.d.cookies)
        status, body, _ = self.d.get("/", extra_headers=hdr)
        self.assertEqual(status, 200)
        self.assertIn("Dashboard", body)


if __name__ == "__main__":
    unittest.main()
