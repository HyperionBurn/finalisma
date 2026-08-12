"""Agent API keys — web UI contract (TDD — RED).

The feature is unusable by a real customer without a page to create/list/
revoke keys. This drives the REAL ``WeftWebApp`` over real HTTP with a session
cookie, following the existing page structure (CSRF-gated forms, _esc'd
rendering, session-cookie auth). The pages do not exist yet — this file must
fail.

Contract under test:

  - /agent-keys is session-cookie-gated (redirects to /login unauthenticated);
  - the page lists keys (label, created, last-used) and NEVER renders the raw
    agk_ credential or its hash;
  - creating a key shows the raw token EXACTLY ONCE in that response, and it is
    not retrievable afterwards;
  - revoking a key removes it from the list;
  - every mutation is CSRF-gated.

Authoritative spec: docs/AGENT_KEYS.md.
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
from weft_cloud.web.app import WeftWebApp  # RED: page absent

SITE_DIR = str(ROOT / "site")


class WebAppDriver:
    """In-process HTTP driver for WeftWebApp (mirrors test_webapp_auth.py)."""

    def __init__(self, smtp_configured: bool | None = False):
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

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        finally:
            self.backend.close()
            self._tmp.cleanup()


class WebAgentKeysBase(unittest.TestCase):
    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"keys{time.time_ns()}@example.com"
        self.password = "agent-key-password-1"

    def tearDown(self):
        self.driver.close()

    def _signup_login(self):
        status, _, _ = self.driver.post(
            "/signup", {"email": self.email, "password": self.password, "_csrf": "x"}
        )
        self.assertEqual(status, 303)
        status, _, _ = self.driver.post(
            "/login", {"email": self.email, "password": self.password, "_csrf": "x"}
        )
        self.assertEqual(status, 303)
        self.assertIn("fss_session", self.driver.cookies)

    def _create_key_via_ui(self, label: str = "ci"):
        status, body, _ = self.driver.get("/agent-keys")
        self.assertEqual(status, 200)
        csrf = self.driver.extract_csrf(body)
        self.assertIsNotNone(csrf)
        status, create_body, _ = self.driver.post(
            "/agent-keys", {"label": label, "_csrf": csrf}
        )
        self.assertEqual(status, 200, f"create via UI failed: {create_body[:200]}")
        m = re.search(r"(agk_[A-Za-z0-9_-]+)", create_body)
        self.assertIsNotNone(m, "creation response must show the raw key once")
        return m.group(1)


class AgentKeyWebPageTests(WebAgentKeysBase):
    def test_page_requires_session_cookie(self):
        status, body, headers = self.driver.get("/agent-keys")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/login"))

    def test_create_shows_raw_key_exactly_once_then_never_again(self):
        self._signup_login()
        raw_key = self._create_key_via_ui("prod")
        self.assertTrue(raw_key.startswith("agk_"))

        # The list page afterwards must NOT contain the raw key (shown once).
        status, body, _ = self.driver.get("/agent-keys")
        self.assertEqual(status, 200)
        self.assertNotIn(raw_key, body, "raw agent key must never be re-rendered")
        self.assertIn("prod", body, "the label should be visible in the list")

    def test_list_shows_metadata_and_not_the_secret(self):
        self._signup_login()
        raw_key = self._create_key_via_ui("staging")
        status, body, _ = self.driver.get("/agent-keys")
        self.assertEqual(status, 200)
        self.assertIn("staging", body)
        self.assertNotIn("token_hash", body)
        self.assertNotIn(raw_key, body)

    def test_revoke_removes_key_from_list(self):
        self._signup_login()
        raw_key = self._create_key_via_ui("to-revoke")

        status, body, _ = self.driver.get("/agent-keys")
        self.assertEqual(status, 200)
        m = re.search(r'name="key_id"\s+value="([^"]+)"', body)
        self.assertIsNotNone(m, "revoke form must carry the key id")
        key_id = m.group(1)
        csrf = self.driver.extract_csrf(body)
        self.assertIsNotNone(csrf)

        status, body, headers = self.driver.post(
            "/agent-keys/revoke", {"key_id": key_id, "_csrf": csrf}
        )
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/agent-keys"))

        status, body, _ = self.driver.get("/agent-keys")
        self.assertEqual(status, 200)
        self.assertNotIn(key_id, body, "revoked key must disappear from the list")
        self.assertNotIn(raw_key, body)

    def test_mutations_are_csrf_gated(self):
        self._signup_login()
        status, body, _ = self.driver.post("/agent-keys", {"label": "x", "_csrf": "wrong"})
        self.assertEqual(status, 403)

        # Seed a real key first, then a forged revoke without a valid CSRF.
        raw_key = self._create_key_via_ui("csrf-holder")
        status, body, _ = self.driver.get("/agent-keys")
        m = re.search(r'name="key_id"\s+value="([^"]+)"', body)
        key_id = m.group(1)
        status, body, _ = self.driver.post("/agent-keys/revoke", {"key_id": key_id, "_csrf": "nope"})
        self.assertEqual(status, 403)
        # Key still listed.
        status, body, _ = self.driver.get("/agent-keys")
        self.assertEqual(status, 200)
        self.assertIn(key_id, body)


if __name__ == "__main__":
    unittest.main()
