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
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def restart(self):
        """Restart only the HTTP app while preserving the test database."""
        import http.server

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.app = WeftWebApp(
            self.backend,
            static_dir=SITE_DIR,
            state_dir=str(Path(self._tmp.name) / "state"),
        )
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.app.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address


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

    def test_create_room_validation_error_returns_400_html(self):
        html = self.driver.get("/rooms")[1]
        csrf = self.driver.extract_csrf(html)
        status, body, _ = self.driver.post(
            "/rooms",
            {"name": "x" * 161, "cap": "8", "_csrf": csrf},
        )
        self.assertEqual(status, 400)
        self.assertIn("name must be at most 160 characters", body)
        self.assertNotIn("Internal server error", body)


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

    def test_room_detail_shows_shareable_link_to_owner_only(self):
        """The raw rm_ join link appears on the detail page ONLY for callers
        entitled to it (owner or active member of THIS room) — never for a
        non-room-member who can reach the page.
        """
        # The owner (an active member) sees the shareable link on creation.
        status, body, _ = self.driver.get(f"/room/{self.room_id}")
        self.assertEqual(status, 200)
        self.assertIsNotNone(
            re.search(r"rm_[A-Za-z0-9_-]+", body),
            "owner must see the shareable join link on the detail page",
        )
        self.assertIn("credential", body, "the page must warn the link is a credential")

    def test_room_detail_uses_configured_public_origin_for_join_link(self):
        previous = os.environ.get("WEFT_PUBLIC_ORIGIN")
        os.environ["WEFT_PUBLIC_ORIGIN"] = "https://rooms.example.test"
        try:
            status, body, _ = self.driver.get(f"/room/{self.room_id}")
        finally:
            if previous is None:
                os.environ.pop("WEFT_PUBLIC_ORIGIN", None)
            else:
                os.environ["WEFT_PUBLIC_ORIGIN"] = previous
        self.assertEqual(status, 200)
        self.assertIn(
            f"https://rooms.example.test/j/",
            body,
            "web-rendered room links must use the configured public origin",
        )
        self.assertNotIn(
            "http://127.0.0.1:18788/j/",
            body,
            "a deployed web page must never advertise the loopback join origin",
        )


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

    def test_lifecycle_events_are_not_treated_as_private_messages(self):
        status, body, _ = self.driver.get(f"/room/{self.room_id}/events?after_seq=0")
        self.assertEqual(status, 200)
        data = json.loads(body)
        lifecycle = [e for e in data["events"] if e["kind"] in {"room.created", "room.joined"}]
        self.assertTrue(lifecycle)
        self.assertTrue(all(e["payload"] for e in lifecycle))
        self.assertNotIn(
            {"redacted": True, "reason": "not_the_addressee"},
            [e["payload"] for e in lifecycle],
        )


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
        status, initial_body, _ = self.driver.get(f"/room/{self.room_id}/connect")
        self.assertEqual(status, 200)
        initial_token = re.search(r"rm_[A-Za-z0-9_-]+", initial_body).group(0)

        # The raw bearer token is intentionally not persisted. A restart must
        # expose a recoverable owner action, never a blank join request.
        self.driver.restart()
        status, connect_body, _ = self.driver.get(f"/room/{self.room_id}/connect")
        self.assertEqual(status, 200)
        self.assertIn("Join link unavailable after a web restart", connect_body)
        self.assertNotIn('link_token": ""', connect_body)
        status, detail_body, _ = self.driver.get(f"/room/{self.room_id}")
        self.assertEqual(status, 200)
        self.assertIn("Join link unavailable after a web restart", detail_body)
        self.assertNotIn('link_token": ""', detail_body)
        csrf = self.driver.extract_csrf(detail_body)
        status, _, headers = self.driver.post(
            f"/room/{self.room_id}/regenerate-link", {"_csrf": csrf}
        )
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].endswith(f"/room/{self.room_id}"))
        regenerated_body = self.driver.get(f"/room/{self.room_id}/connect")[1]
        regenerated_token = re.search(r"rm_[A-Za-z0-9_-]+", regenerated_body).group(0)
        self.assertNotEqual(regenerated_token, initial_token)
        self.assertIsNone(self.driver.app.rooms.resolve_room_by_link_token(initial_token))

        # Concurrent owner clicks must leave the process cache aligned with
        # whichever hash was committed last, not with an already-invalid token.
        csrf = self.driver.extract_csrf(self.driver.get(f"/room/{self.room_id}")[1])
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(
                lambda _: self.driver.post(
                    f"/room/{self.room_id}/regenerate-link", {"_csrf": csrf}
                ),
                range(2),
            ))
        self.assertEqual([response[0] for response in responses], [303, 303])
        cached_token = self.driver.app._get_room_link_token(self.room_id)
        self.assertIsNotNone(cached_token)
        self.assertEqual(
            self.driver.app.rooms.resolve_room_by_link_token(cached_token),
            self.room_id,
        )
        status, _, _ = self.driver.get(f"/room/{self.room_id}/regenerate-link")
        self.assertEqual(status, 404)

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
        self._assert_connector_configs_use_portable_entrypoint()
        self._assert_authenticated_pages_have_accessible_landmarks()

    def _assert_connector_configs_use_portable_entrypoint(self):
        from html import unescape

        for client in ("claude-desktop", "cursor", "codex"):
            csrf = self.driver.csrf("/config")
            status, body, headers = self.driver.post(
                "/config", {"client": client, "_csrf": csrf}
            )
            self.assertEqual(status, 200, client)
            self.assertEqual(headers.get("Cache-Control"), "no-store")
            match = re.search(r"<pre><code>(.*?)</code></pre>", body, re.S)
            self.assertIsNotNone(match, client)
            config_text = unescape(match.group(1))
            self.assertIn("-m", config_text)
            self.assertIn("weft_mcp", config_text)
            self.assertNotIn("scripts/weft-mcp.py", config_text)
            self.assertNotIn("/app/", config_text)
            self.assertNotIn("\\scripts\\", config_text)
            if client != "codex":
                payload = json.loads(config_text)
                args = payload["mcpServers"]["weft"]["args"]
                self.assertEqual(args[:3], ["-B", "-m", "weft_mcp"])
                self.assertFalse(any(arg.startswith("agk_") for arg in args))
                self.assertIn("WEFT_TOKEN", payload["mcpServers"]["weft"]["env"])
            else:
                self.assertIn('args = ["-B", "-m", "weft_mcp"', config_text)
                self.assertIn("WEFT_TOKEN", config_text)

    def _assert_authenticated_pages_have_accessible_landmarks(self):
        paths = (
            "/",
            "/org",
            "/rooms",
            "/agent-keys",
            "/config",
            f"/room/{self.room_id}",
            f"/room/{self.room_id}/connect",
            f"/room/{self.room_id}/audit",
        )
        for path in paths:
            status, body, _ = self.driver.get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(
                body.count('<a class="skip-link" href="#main">Skip to content</a>'),
                1,
                path,
            )
            self.assertEqual(body.count('<main id="main" tabindex="-1">'), 1, path)

    def test_authenticated_metadata_text_meets_wcag_aa_contrast(self):
        status, body, _ = self.driver.get("/")
        self.assertEqual(status, 200)
        self.assertIn(".muted{color:#707070;}", body)
        self.assertNotIn(".muted{color:#777;}", body)
        self.assertIn("border:1px solid #767676;", body)
        self.assertIn("border:1px solid #707070;", body)

    def test_connect_page_documents_a_working_join(self):
        """The page must document everything a working /v1/rooms/join needs.

        Regression for the bug where Tier 2 printed only
        ``POST /v1/rooms/join {"room_id","link_token"}`` — a copy-paste failed
        with 401 (missing bearer) then 400 (missing consent).
        """
        status, body, _ = self.driver.get(f"/room/{self.room_id}/connect")
        self.assertEqual(status, 200)
        # The join endpoint.
        self.assertIn("POST /v1/rooms/join", body)
        # The auth header a working call requires.
        self.assertIn("Authorization: Bearer", body)
        # Consent, including the literal boolean form the API enforces.
        self.assertIn("consent", body)
        self.assertIn("consent: true", body)
        # How to obtain the session token: signup/signin steps.
        self.assertIn("/v1/auth/signup", body)
        self.assertIn("/v1/auth/signin", body)
        self.assertIn(
            '<pre tabindex="0" role="region" aria-label="Code example">',
            body,
        )
        # The product promise: joining is cross-tenant; the link is the authz.
        self.assertIn("cross-tenant", body)
        # Consent is a stored caller attestation, NOT proof a human approved.
        self.assertIn("attestation", body)
        self.assertIn("not proof that a human saw and approved", body)

    def test_connect_page_does_not_leak_credentials(self):
        """The connect page renders the link token but no credentials."""
        status, body, _ = self.driver.get(f"/room/{self.room_id}/connect")
        self.assertEqual(status, 200)
        session_cookie = self.driver.cookies.get("fss_session", "")
        for banned in (self.password, session_cookie, self.email):
            if banned:
                self.assertNotIn(banned, body,
                                 f"connect page leaked a credential: {banned!r}")

    def test_unauthenticated_connect_redirects_to_login(self):
        # No cookie yet: the page must not render — it must send to /login.
        anon = WebAppDriver()
        try:
            status, body, headers = anon.get(f"/room/{self.room_id}/connect")
            self.assertEqual(status, 303)
            self.assertTrue(headers.get("Location", "").startswith("/login"))
            self.assertNotIn("POST /v1/rooms/join", body)
        finally:
            anon.close()


class TestConnectPageRequestShapeDrivesRealJoin(unittest.TestCase):
    """The STRONGEST guard: extract the request shape the connect page documents
    and drive the REAL /v1/rooms/join handler with it, asserting success.

    This is the assertion that would have caught the original bug: the page
    documented a request that the real handler rejected. It also proves the
    cross-tenant promise end to end — a brand-new agent's org redeems the
    owner's link.
    """

    def setUp(self):
        self.driver = WebAppDriver()
        self.email = f"owner{time.time_ns()}@example.com"
        self.password = "owner-password-ok"
        self.driver.login(self.email, self.password)
        self.room_id = self.driver.create_room(name="War Room")

    def tearDown(self):
        if hasattr(self, "_httpd"):
            try:
                self._httpd.shutdown()
            finally:
                self._httpd.server_close()  # release the listening socket
        self.driver.close()

    def _start_cloud_service(self):
        """Serve the REAL WeftCloudService over the SAME backend as the web app."""
        import http.server
        from weft_cloud.service import WeftCloudService, _CloudHTTPHandler

        self.cloud = WeftCloudService(self.driver.backend,
                                      origin=f"http://127.0.0.1:{self.driver.port}")
        _CloudHTTPHandler.service = self.cloud
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.cloud_port = self._httpd.server_address[1]
        self._cloud_thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
        )
        self._cloud_thread.start()

    def _cloud_post(self, path: str, body: dict, token: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.cloud_port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("POST", path, body=json.dumps(body), headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        conn.close()
        payload = {}
        try:
            payload = json.loads(raw)
        except Exception:
            pass
        return resp.status, payload

    def test_documented_request_shape_drives_real_join_handler(self):
        # 1. Render the connect page and confirm it documents the working call.
        status, page, _ = self.driver.get(f"/room/{self.room_id}/connect")
        self.assertEqual(status, 200)
        self.assertIn("POST /v1/rooms/join", page)
        self.assertIn("Authorization: Bearer", page)
        self.assertIn("consent: true", page)

        # 2. Extract the exact request shape the page documents.
        room_match = re.search(r'"room_id": "(room_[A-Za-z0-9_-]+)"', page)
        token_match = re.search(r'"link_token": "(rm_[A-Za-z0-9_-]+)"', page)
        self.assertIsNotNone(room_match, "page must document room_id in the join body")
        self.assertIsNotNone(token_match, "page must document link_token in the join body")
        doc_room_id = room_match.group(1)
        doc_link_token = token_match.group(1)
        self.assertEqual(doc_room_id, self.room_id)

        # 3. The room's owning tenant (to prove the join is cross-tenant).
        with self.driver.backend.transaction() as tx:
            row = tx.execute(
                "SELECT tenant_id FROM cloud_rooms WHERE room_id = ?", (self.room_id,)
            ).fetchone()
        room_tenant = row["tenant_id"]

        # 4. Drive the REAL join handler with the page's shape.
        self._start_cloud_service()
        agent_email = f"agent{time.time_ns()}@example.com"
        signup_status, signup = self._cloud_post("/v1/auth/signup", {
            "email": agent_email, "password": "AgentPass!1",
        })
        self.assertEqual(signup_status, 201, f"signup failed: {signup}")
        # The agent's org is brand-new, so this is a cross-tenant redeem.
        self.assertNotEqual(signup["tenant_id"], room_tenant)
        # Identity comes from the authenticated session — the documented shape
        # carries no agent_id argument.
        join_status, joined = self._cloud_post("/v1/rooms/join", {
            "room_id": doc_room_id,
            "link_token": doc_link_token,
            "consent": True,
            "capabilities": [],
        }, token=signup["session_token"])
        self.assertEqual(join_status, 200, f"join with documented shape failed: {joined}")
        self.assertEqual(joined["status"], "active")
        self.assertEqual(joined["room_id"], self.room_id)

    def test_documented_shape_without_auth_is_still_rejected(self):
        # The page must not have weakened the API: a bearer is still required.
        status, page, _ = self.driver.get(f"/room/{self.room_id}/connect")
        self.assertEqual(status, 200)
        self._start_cloud_service()
        room_match = re.search(r'"room_id": "(room_[A-Za-z0-9_-]+)"', page)
        token_match = re.search(r'"link_token": "(rm_[A-Za-z0-9_-]+)"', page)
        status, body = self._cloud_post("/v1/rooms/join", {
            "room_id": room_match.group(1),
            "link_token": token_match.group(1),
            "agent_id": "my-agent",
            "consent": True,
        })
        self.assertEqual(status, 401)
        self.assertEqual(body.get("error", {}).get("code"), "unauthorized")


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
        self.driver.accept_invite(token, self.member_email, self.member_password)
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
    """§3.3: a member can VIEW a room's detail page (read-only, no bearer secrets).

    The room DETAIL page is deliberately viewable by any org member (roster +
    redacted event log). The CONNECT page is different: it hands out the room's
    raw rm_ link token — a multi-use bearer capability — so it requires the
    caller to be the room's OWNER or an ACTIVE MEMBER of THAT room. A same-org
    member who was invited to the org but never to the room gets the identical
    404 as a room that does not exist (no existence oracle).
    """

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
        self.driver.accept_invite(token, self.member_email, self.member_password)
        self.driver.cookies.clear()
        self.driver.post("/login", {"email": self.member_email, "password": self.member_password})

    def tearDown(self):
        if hasattr(self, "_cloud_httpd"):
            try:
                self._cloud_httpd.shutdown()
            finally:
                self._cloud_httpd.server_close()
        self.driver.close()

    def _start_cloud_service(self):
        """Serve the REAL WeftCloudService over the SAME backend as the web app."""
        from weft_cloud.service import WeftCloudService, _CloudHTTPHandler

        self._cloud = WeftCloudService(self.driver.backend,
                                       origin=f"http://127.0.0.1:{self.driver.port}")
        _CloudHTTPHandler.service = self._cloud
        self._cloud_httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self._cloud_port = self._cloud_httpd.server_address[1]
        self._cloud_thread = threading.Thread(
            target=self._cloud_httpd.serve_forever, daemon=True,
        )
        self._cloud_thread.start()

    def _cloud_post(self, path: str, body: dict, token: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self._cloud_port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("POST", path, body=json.dumps(body), headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        conn.close()
        payload = {}
        try:
            payload = json.loads(raw)
        except Exception:
            pass
        return resp.status, payload

    def test_member_can_view_room_detail(self):
        status, body, _ = self.driver.get(f"/room/{self.room_id}")
        self.assertEqual(status, 200)
        self.assertIn("War Room", body)

    def test_member_cannot_view_connect_page_with_link_token(self):
        # A same-org member who was invited to the org but NEVER to this room
        # must not receive the raw rm_ bearer token. The refusal is the
        # identical 404 a nonexistent room returns (no existence oracle).
        status, body, _ = self.driver.get(f"/room/{self.room_id}/connect")
        self.assertEqual(status, 404)
        self.assertIsNone(re.search(r"rm_[A-Za-z0-9_-]+", body),
                          "connect page must not leak the link token to a non-room-member")

    def test_connect_refusal_is_identical_to_nonexistent_room(self):
        # The refusal for "room exists but you're not in it" must be byte-for-byte
        # identical to the refusal for a room that never existed, or the endpoint
        # becomes a room-existence oracle (the repo has already fixed three of
        # these; this must not be a fourth).
        status_real, body_real, _ = self.driver.get(f"/room/{self.room_id}/connect")
        status_fake, body_fake, _ = self.driver.get("/room/room_0000deadbeef/connect")
        self.assertEqual(status_real, 404)
        self.assertEqual(status_fake, 404)
        self.assertEqual(body_real, body_fake,
                         "refusal for a real non-member room must be identical "
                         "to the refusal for a nonexistent room")

    def test_active_room_member_can_view_connect_page(self):
        # The entitlement rule is "OWNER or ACTIVE MEMBER of THIS room", so a
        # same-org member who actually JOINS the room via its link regains the
        # connect page (they are entitled to share the link they redeemed).
        # 1. Recover the raw link token as the owner.
        saved_cookie = dict(self.driver.cookies)
        self.driver.cookies = {}
        self.driver.post("/login", {"email": self.owner_email, "password": self.owner_password})
        owner_status, owner_body, _ = self.driver.get(f"/room/{self.room_id}/connect")
        self.assertEqual(owner_status, 200)
        link_token = re.search(r"(rm_[A-Za-z0-9_-]+)", owner_body).group(1)
        self.driver.cookies = saved_cookie

        # 2. Join the room as the member through the REAL /v1/rooms/join handler.
        self._start_cloud_service()
        signin_status, signin = self._cloud_post("/v1/auth/signin", {
            "email": self.member_email, "password": self.member_password,
        })
        self.assertEqual(signin_status, 200, f"member signin failed: {signin}")
        session_token = signin["session_token"]
        join_status, joined = self._cloud_post("/v1/rooms/join", {
            "room_id": self.room_id,
            "link_token": link_token,
            "consent": True,
            "capabilities": [],
        }, token=session_token)
        self.assertEqual(join_status, 200, f"member join failed: {joined}")

        # 3. Now an active room member: the connect page renders the link token.
        status, body, _ = self.driver.get(f"/room/{self.room_id}/connect")
        self.assertEqual(status, 200)
        self.assertIsNotNone(re.search(r"rm_[A-Za-z0-9_-]+", body),
                             "an active room member must be able to share the link")


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
        self.driver_a.accept_invite(token, self.member_a, self.member_a_password)
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
