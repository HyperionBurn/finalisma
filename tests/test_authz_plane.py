"""Authorization hole regression tests for the /v1 REST plane.

The four exploitable-now holes this suite locks shut:
  1. passwordless account takeover via signup (existing email -> session)
  2. cross-tenant impersonation via client-supplied ``agent_id``
  3. ``GET /v1/rooms?agent_id=<victim>`` room enumeration across tenants
  4. ``event_log`` returning unredacted unicast payloads to non-addressees

Every test drives the REAL ``WeftCloudService`` over REAL HTTP on a
background thread — never an internal function. Nothing calls the service
methods directly, so each test FAILS if the wiring is ever removed again.

The no-oracle property (a foreign room must look identical to a fabricated
one) is asserted by comparing the real-vs-fake responses to EACH OTHER, so a
future change that re-splits the codes into two NEW distinct values still
fails even if both differ from today's literal.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        finally:
            exc.close()  # release the unread response body / socket
        return exc.code, payload


def _get(base: str, path: str, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        finally:
            exc.close()  # release the unread response body / socket
        return exc.code, payload


class AuthzPlaneTestBase(unittest.TestCase):
    """Real HTTP service on a background thread, one fresh DB per test."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="weft-authz-test-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        from http.server import ThreadingHTTPServer

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.service = WeftCloudService(SqliteWalBackend(self.db_path))
        _CloudHTTPHandler.service = self.service
        self.server = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
        )
        self.server.start()

    def tearDown(self) -> None:
        # shutdown() stops serve_forever but does NOT close the listening
        # socket; server_close() is what releases the port. Both must run even
        # when a test failed, or a leaked listener can hijack a later test's
        # connections (they handshake then hang -> TimeoutError).
        if hasattr(self, "_httpd"):
            try:
                self._httpd.shutdown()
            finally:
                self._httpd.server_close()
        try:
            self.service.backend.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _signup(self, email: str, password: str = "CorrectHorse!1") -> dict:
        status, body = _post(self.base, "/v1/auth/signup", {
            "email": email, "password": password,
        })
        self.assertEqual(status, 201, f"signup failed: {body}")
        return body

    def _signup_raw(self, email: str, password: str = "CorrectHorse!1",
                    tenant_id: str | None = None) -> tuple[int, dict]:
        body: dict = {"email": email, "password": password}
        if tenant_id is not None:
            body["tenant_id"] = tenant_id
        return _post(self.base, "/v1/auth/signup", body)

    def _create_room(self, token: str, cap: int = 6, **kwargs) -> dict:
        status, body = _post(self.base, "/v1/rooms/create", {
            "cap": cap, **kwargs,
        }, token)
        self.assertEqual(status, 201, f"room create failed: {body}")
        return body

    def _join_room(self, token: str, room_id: str, link_token: str) -> dict:
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room_id,
            "link_token": link_token,
            "consent": True,
        }, token)
        self.assertEqual(status, 200, f"join failed: {body}")
        return body

    def _signin(self, email: str, password: str) -> tuple[int, dict]:
        return _post(self.base, "/v1/auth/signin", {
            "email": email, "password": password,
        })


class TestSignupCannotTakeoverAnAccount(AuthzPlaneTestBase):
    """HOLE 1 — passwordless account takeover via signup."""

    def test_signup_with_existing_email_returns_no_session(self) -> None:
        victim = self._signup("victim@example.com", "VictimPass!1")

        # Posting the victim's email with ANY password must NOT return a
        # session (or any account id) for the victim's account.
        status, body = self._signup_raw("victim@example.com", "attacker-pw-1")
        self.assertNotEqual(status, 201, "duplicate signup must not succeed")
        self.assertNotIn("session_token", body,
                         "duplicate signup must not mint a session for the victim")
        self.assertNotIn("account_id", body)
        self.assertEqual(body["error"]["code"], "email_exists")

        # The victim's own password is unchanged and still authenticates to
        # the SAME account — nothing about the victim was altered.
        status, signin = self._signin("victim@example.com", "VictimPass!1")
        self.assertEqual(status, 200)
        self.assertEqual(signin["account_id"], victim["account_id"])
        self.assertEqual(signin["tenant_id"], victim["tenant_id"])

        # The attacker's password does NOT work — no account was created.
        status, signin = self._signin("victim@example.com", "attacker-pw-1")
        self.assertEqual(status, 401)

    def test_signup_cannot_choose_its_own_tenant(self) -> None:
        # A real tenant exists first.
        existing = self._signup("existing@example.com", "ExistingPass!1")
        target_tenant = existing["tenant_id"]

        # The exact reported attack: brand-new email + org_name + the VICTIM's
        # tenant_id. The server must mint a FRESH tenant and ignore the
        # client-chosen one — and the issued session must NOT be able to read
        # the victim organisation's member roster.
        status, body = self._signup_raw("newbie@example.com", "NewbiePass!1",
                                        tenant_id=target_tenant)
        self.assertEqual(status, 201)
        self.assertNotEqual(body["tenant_id"], target_tenant,
                            "signup must not admit the caller to a client-chosen tenant")
        self.assertTrue(body["tenant_id"].startswith("tenant_"))
        self.assertNotEqual(body["account_id"], existing["account_id"])

        # The new user is NOT a member of the existing tenant, and cannot read
        # its roster with the issued session: /v1/org/members returns only the
        # attacker's own membership in their fresh tenant — never the victim's.
        status, members = _get(self.base, "/v1/org/members", body["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(len(members["members"]), 1)
        self.assertEqual(members["members"][0]["account_id"], body["account_id"])
        self.assertNotEqual(members["members"][0]["account_id"], existing["account_id"])
        self.assertNotIn("existing@example.com",
                         json.dumps(members),
                         "attacker's session must not expose the victim org's roster")


class TestCrossTenantImpersonationSealed(AuthzPlaneTestBase):
    """HOLE 2 — client-supplied agent_id cannot reach another tenant's room.

    An attacker in tenant B who holds a room_id and a member's agent_id from
    tenant A, but NOT that member's credential, gets the SAME uniform
    ``room_not_found`` for the real room and a fabricated one — no oracle.
    """

    def test_foreign_member_agent_id_is_sealed_and_no_oracle(self) -> None:
        alice = self._signup("alice@example.com", "AlicePass!1")
        room = self._create_room(alice["session_token"], cap=6)
        room_id = room["room_id"]
        bob = self._signup("bob@example.com", "BobPass!1")

        fake_room_id = "room_" + "f" * 32

        # HOLE 2: an attacker in tenant B stuffs a tenant-A member's account_id
        # into every identity argument. Identity is derived from the
        # authenticated session — NEVER the request body — so each argument is
        # refused outright, and the refusal is byte-identical for a real room
        # and a fabricated one (no existence oracle).
        attempts = [
            ("POST", "/v1/rooms/poll", {"room_id": room_id, "agent_id": alice["account_id"]}),
            ("POST", "/v1/rooms/send", {
                "room_id": room_id, "sender_agent_id": alice["account_id"],
                "target_spec": "*", "payload": {"text": "forged"}}),
            ("POST", "/v1/rooms/close", {
                "room_id": room_id, "caller_agent_id": alice["account_id"]}),
            ("POST", "/v1/rooms/leave", {
                "room_id": room_id, "agent_id": alice["account_id"]}),
            ("POST", "/v1/rooms/event_log", {
                "room_id": room_id, "agent_id": alice["account_id"]}),
            ("POST", "/v1/rooms/heartbeat", {
                "room_id": room_id, "agent_id": alice["account_id"]}),
            ("POST", "/v1/rooms/ack", {
                "room_id": room_id, "agent_id": alice["account_id"], "seq": 1}),
        ]

        for method, path, body in attempts:
            with self.subTest(path=path):
                # Real room, smuggled identity.
                s_real, b_real = self._do(method, path, body, bob["session_token"])
                # Fabricated room, same smuggled identity.
                fake_body = dict(body)
                fake_body["room_id"] = fake_room_id
                s_fake, b_fake = self._do(method, path, fake_body, bob["session_token"])
                self.assertEqual(s_real, s_fake,
                                 f"{path}: real vs fake room must not differ")
                self.assertEqual(b_real, b_fake,
                                 f"{path}: real vs fake room must be byte-identical (no oracle)")
                self.assertEqual(s_real, 400)
                self.assertEqual(b_real["error"]["code"], "invalid_argument")
                self.assertIn("not accepted", b_real["error"]["message"])

        # room_info goes through a GET query string; the query-param agent_id
        # is IGNORED (never honoured) and the caller resolves as its OWN
        # account — bob, not a member -> uniform 404 for real and fake alike.
        s_real, b_real = _get(self.base,
                              f"/v1/rooms/info?{urlencode({'room_id': room_id, 'agent_id': alice['account_id']})}",
                              bob["session_token"])
        s_fake, b_fake = _get(self.base,
                              f"/v1/rooms/info?{urlencode({'room_id': fake_room_id, 'agent_id': alice['account_id']})}",
                              bob["session_token"])
        self.assertEqual(s_real, s_fake)
        self.assertEqual(b_real, b_fake)
        self.assertEqual(s_real, 404)

    def test_link_join_still_admits_a_real_cross_tenant_member(self) -> None:
        """The fix must NOT break the legitimate cross-tenant link capability:
        an outsider who JOINS via the link can then operate as a member under
        their own authenticated account."""
        alice = self._signup("alice2@example.com", "AlicePass!1")
        room = self._create_room(alice["session_token"], cap=6)
        bob = self._signup("bob2@example.com", "BobPass!1")

        # Before joining, bob is sealed — a uniform room_not_found.
        s, b = _post(self.base, "/v1/rooms/poll",
                     {"room_id": room["room_id"]},
                     bob["session_token"])
        self.assertEqual(s, 404)

        # Bob joins via the real link under his own account.
        joined = self._join_room(bob["session_token"], room["room_id"], room["link_token"])
        self.assertEqual(joined["status"], "active")

        # Now bob can operate — resolved through HIS membership (his account).
        s, b = _post(self.base, "/v1/rooms/poll",
                     {"room_id": room["room_id"]},
                     bob["session_token"])
        self.assertEqual(s, 200)
        self.assertEqual(b["room_id"], room["room_id"])

    def _do(self, method: str, path: str, body: dict, token: str) -> tuple[int, dict]:
        if method == "POST":
            return _post(self.base, path, body, token)
        return _get(self.base, path + "?" + urlencode(body), token)


class TestListRoomsScopedToCaller(AuthzPlaneTestBase):
    """HOLE 3 — GET /v1/rooms returns only the caller's own rooms."""

    def test_list_rooms_ignores_client_agent_id(self) -> None:
        alice = self._signup("list-alice@example.com", "AlicePass!1")
        room_a = self._create_room(alice["session_token"], cap=6, name="alice-room")
        bob = self._signup("list-bob@example.com", "BobPass!1")
        room_b = self._create_room(bob["session_token"], cap=6, name="bob-room")

        # Each caller sees only their own rooms.
        status, body = _get(self.base, "/v1/rooms", alice["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual([r["room_id"] for r in body["rooms"]], [room_a["room_id"]])

        status, body = _get(self.base, "/v1/rooms", bob["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual([r["room_id"] for r in body["rooms"]], [room_b["room_id"]])

        # The attack: alice asks for BOB's rooms by supplying bob's account_id.
        # The query param must be ignored — alice still sees only her own.
        status, body = _get(self.base,
                            f"/v1/rooms?{urlencode({'agent_id': bob['account_id']})}",
                            alice["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual([r["room_id"] for r in body["rooms"]], [room_a["room_id"]],
                         "agent_id query parameter must be ignored")

        # A member's own rooms across tenants: alice joins bob's room via the
        # link under her OWN account identity (the /v1 join pattern) and now
        # sees both of her memberships.
        self._join_room(alice["session_token"], room_b["room_id"], room_b["link_token"])
        status, body = _get(self.base, "/v1/rooms", alice["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(sorted(r["room_id"] for r in body["rooms"]),
                         sorted([room_a["room_id"], room_b["room_id"]]))


class TestEventLogRedactsLikePoll(AuthzPlaneTestBase):
    """HOLE 4 — event_log redacts a unicast body for a non-addressee exactly
    as poll does."""

    def setUp(self) -> None:
        super().setUp()
        alice = self._signup("evt-alice@example.com", "AlicePass!1")
        self.alice_token = alice["session_token"]
        self.alice_id = alice["account_id"]
        self.room = self._create_room(alice["session_token"], cap=6)
        carol = self._signup("evt-carol@example.com", "CarolPass!1")
        self.carol_token = carol["session_token"]
        self.carol_id = carol["account_id"]
        self._join_room(self.carol_token, self.room["room_id"], self.room["link_token"])

        # Alice sends a unicast to carol's account.
        status, body = _post(self.base, "/v1/rooms/send", {
            "room_id": self.room["room_id"],
            "target_spec": self.carol_id,
            "payload": {"text": "TOP-SECRET-UNICAST"},
        }, self.alice_token)
        self.assertEqual(status, 200)

    def _message_events(self, result: dict) -> list[dict]:
        return [e for e in result["events"] if e["kind"] == "room.message"]

    def test_poll_and_event_log_redact_identically(self) -> None:
        # The addressee sees the body in BOTH surfaces.
        _, poll_addressee = _post(self.base, "/v1/rooms/poll", {
            "room_id": self.room["room_id"], "after_seq": 0,
        }, self.carol_token)
        self.assertEqual(poll_addressee["events"][-1]["payload"]["payload"]["text"],
                         "TOP-SECRET-UNICAST")
        _, log_addressee = _post(self.base, "/v1/rooms/event_log", {
            "room_id": self.room["room_id"],
        }, self.carol_token)
        self.assertEqual(log_addressee["events"][-1]["payload"]["payload"]["text"],
                         "TOP-SECRET-UNICAST")

        # A NON-addressee sees the redacted envelope in BOTH surfaces, and the
        # secret never appears in the serialized response.
        _, poll_outsider = _post(self.base, "/v1/rooms/poll", {
            "room_id": self.room["room_id"], "after_seq": 0,
        }, self.alice_token)
        poll_msg = self._message_events(poll_outsider)[-1]
        self.assertEqual(poll_msg["payload"],
                         {"redacted": True, "reason": "not_the_addressee"})

        _, log_outsider = _post(self.base, "/v1/rooms/event_log", {
            "room_id": self.room["room_id"],
        }, self.alice_token)
        log_msg = self._message_events(log_outsider)[-1]
        self.assertEqual(log_msg["payload"],
                         {"redacted": True, "reason": "not_the_addressee"})

        self.assertNotIn("TOP-SECRET-UNICAST", json.dumps(poll_outsider))
        self.assertNotIn("TOP-SECRET-UNICAST", json.dumps(log_outsider))
        self.assertNotIn("payload_json", json.dumps(log_outsider),
                         "event_log must never return the raw payload_json column")


if __name__ == "__main__":
    unittest.main()
