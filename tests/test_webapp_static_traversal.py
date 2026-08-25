"""Static-file path containment — negative contract (Wave H supplement).

The static handler must never serve a file whose resolved path lies outside
the static root, regardless of how the traversal is encoded: `..` segments,
percent-encoded separators, backslashes, drive letters, or symlinks. The fix
is resolve-then-assert containment, never a blocklist.

Each test drives the real HTTP surface with an authenticated session and a
controlled temp static directory, so every assertion is measurable.
"""

from __future__ import annotations
from tests._server_readiness import await_serving as _await_serving

import http.client
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.storage import SqliteWalBackend
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.web.app import WeftWebApp


class StaticDriver:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.static_dir = root / "static"
        self.static_dir.mkdir()
        (self.static_dir / "index.html").write_text("ROOT_INDEX", encoding="utf-8")
        nested = self.static_dir / "nested"
        nested.mkdir()
        (nested / "page.html").write_text("NESTED_PAGE", encoding="utf-8")
        # A decoy OUTSIDE the static root — the traversal must never reach it.
        self.secret = root / "secret.txt"
        self.secret.write_text("SECRET_LEAKED", encoding="utf-8")
        self.backend = SqliteWalBackend(str(root / "cloud.db"))
        self.backend.initialize()
        ensure_schema(self.backend)
        self.app = WeftWebApp(
            self.backend,
            static_dir=str(self.static_dir),
            state_dir=str(root / "state"),
        )
        import http.server
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.app.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _await_serving(self.server)
        self.host, self.port = self.server.server_address
        self.cookies: dict[str, str] = {}

    def _headers(self, cookie: str | None):
        if cookie is not None:
            return {"Cookie": "fss_session=" + cookie}
        if self.cookies:
            return {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        return {}

    def _collect_cookies(self, resp):
        for hdr, val in resp.getheaders():
            if hdr.lower() == "set-cookie":
                kv = val.split(";")[0]
                k, v = kv.split("=", 1)
                if "Max-Age=0" in val or v.strip() == "":
                    self.cookies.pop(k.strip(), None)
                else:
                    self.cookies[k.strip()] = v.strip()

    def request(self, method: str, raw_target: str, cookie: str | None = None,
                body: bytes | None = None, content_type: str | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = self._headers(cookie)
        if content_type:
            headers["Content-Type"] = content_type
        conn.request(method, raw_target, body=body, headers=headers)
        resp = conn.getresponse()
        self._collect_cookies(resp)
        data = resp.read()
        out = dict(resp.getheaders())
        conn.close()
        return resp.status, data, out

    def get(self, raw_target, cookie=None):
        return self.request("GET", raw_target, cookie=cookie)

    def _csrf_for_post(self, raw_target: str, form: dict) -> str:
        path = raw_target.split("?", 1)[0]
        page = path
        if path in {"/verify", "/reset"}:
            page = f"{path}?token={quote(str(form.get('token', '')), safe='')}"
        status, body, _ = self.get(page)
        if status != 200:
            raise AssertionError(f"CSRF form page {page} returned {status}")
        match = re.search(rb'name="_csrf"\s+value="([^"]+)"', body)
        if not match:
            raise AssertionError(f"CSRF form page {page} did not contain a token")
        return match.group(1).decode("ascii")

    def post(self, raw_target, form, cookie=None, *, auto_csrf: bool = True):
        form = dict(form)
        path = raw_target.split("?", 1)[0]
        if auto_csrf and "_csrf" not in form and path in {
            "/signup", "/login", "/verify", "/reset-request", "/reset",
        }:
            form["_csrf"] = self._csrf_for_post(raw_target, form)
        return self.request("POST", raw_target, cookie=cookie,
                            body=urlencode(form).encode(),
                            content_type="application/x-www-form-urlencoded")

    def auth_session(self):
        """Signup+verify+login; returns the raw session cookie or None."""
        self.post("/signup", {"email": "s@example.com", "password": "Password123!"})
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT body FROM cloud_identity_outbox WHERE to_email = ? ORDER BY created_at DESC LIMIT 1",
                ("s@example.com",),
            ).fetchone()
        m = re.search(r"(fvt_[A-Za-z0-9_-]+)", row["body"])
        self.post("/verify", {"token": m.group(1)})
        status, _, _ = self.post("/login", {"email": "s@example.com", "password": "Password123!"})
        return self.cookies.get("fss_session") if status == 303 else None

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        finally:
            self.backend.close()
            self._tmp.cleanup()


class TestStaticTraversalContainment(unittest.TestCase):
    def setUp(self):
        self.d = StaticDriver()
        self.cookie = self.d.auth_session()
        self.assertIsNotNone(self.cookie, "session required to reach static files")

    def tearDown(self):
        self.d.close()

    def _assert_not_served(self, raw_target):
        """The request must not return 200 with decoy/leak content."""
        status, body, _ = self.d.get(raw_target, cookie=self.cookie)
        self.assertNotEqual(status, 200, f"{raw_target} escaped and returned 200")
        self.assertNotIn(b"SECRET_LEAKED", body, f"{raw_target} leaked the decoy file")

    def test_legit_index_served(self):
        status, body, _ = self.d.get("/index.html", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ROOT_INDEX")

    def test_legit_nested_file_served(self):
        status, body, _ = self.d.get("/nested/page.html", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"NESTED_PAGE")

    def test_dotdot_slash_traversal_rejected(self):
        self._assert_not_served("/../secret.txt")

    def test_dotdot_encoded_slash_traversal_rejected(self):
        self._assert_not_served("/..%2fsecret.txt")
        self._assert_not_served("/%2e%2e%2fsecret.txt")

    def test_dotdot_encoded_backslash_traversal_rejected(self):
        self._assert_not_served("/..%5csecret.txt")
        self._assert_not_served("/%2e%2e%5csecret.txt")

    def test_backslash_traversal_rejected(self):
        self._assert_not_served("/..\\..\\secret.txt")
        self._assert_not_served("/\\..\\..\\secret.txt")

    def test_windows_winini_backslash_rejected(self):
        # `\Windows\win.ini` is rooted on Windows and previously escaped.
        self._assert_not_served("/\\Windows\\win.ini")
        self._assert_not_served("/\\windows\\win.ini")

    @unittest.skipUnless(os.name == "nt", "drive-letter paths only exist on Windows")
    def test_drive_letter_absolute_rejected(self):
        self._assert_not_served("/C:\\Windows\\win.ini")
        self._assert_not_served("/C:/Windows/win.ini")

    def test_double_slash_absolute_rejected(self):
        self._assert_not_served("//etc/passwd")
        self._assert_not_served("/etc/passwd")

    def test_symlink_escaping_root_rejected(self):
        # Only meaningful where symlinks can be created without elevation.
        link = self.d.static_dir / "esc.html"
        try:
            os.symlink(self.d.secret, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted on this host")
        try:
            status, body, _ = self.d.get("/esc.html", cookie=self.cookie)
            self.assertNotEqual(status, 200)
            self.assertNotIn(b"SECRET_LEAKED", body)
        finally:
            link.unlink(missing_ok=True)

    def test_encoded_absolute_drive_rejected(self):
        # Windows rooted absolute with an encoded separator mix.
        self._assert_not_served("/C:%5cWindows%5cwin.ini")


if __name__ == "__main__":
    unittest.main()
