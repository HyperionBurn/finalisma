"""RED deliverable — room membership is bound to the ACCOUNT, not the session token.

Regression: ``/v1`` room membership was bound to ``actor_token_hash`` (the
SHA-256 of the caller's SESSION token). A session token changes on every
login, so re-signing-in silently revoked every room the caller had joined:

    join with session #1 -> 200
    sign in again         -> session #2
    poll with session #2  -> 404 room_not_found   (SAME human, SAME room)

This suite locks the fix and its two security invariants:

1. MEMBERSHIP SURVIVES RE-LOGIN — join/create with session #1, sign in again,
   and every room operation (poll, send, info, event_log, ack, heartbeat,
   wait, leave) still works with session #2. Identity follows the account,
   which never changes, not the credential, which does.

2. CROSS-TENANT IMPERSONATION STAYS BLOCKED — the identity used to authorize
   a room operation comes from the authenticated session (``ctx``) and NEVER
   from the request body. A caller in tenant B who stuffs a tenant-A member's
   agent_id into the body cannot poll, send, close, leave, or read the event
   log of that room — the identity argument is refused outright.

3. NO ENUMERATION ORACLE — a non-member's refusal for a REAL room is
   byte-identical to a fabricated room, and the two responses are asserted
   equal to EACH OTHER (never against a literal), so a re-split of the error
   codes still fails this suite.

Every test drives the real ``WeftCloudService`` over real HTTP.
"""

from __future__ import annotations
from tests._server_readiness import await_serving as _await_serving

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler  # noqa: E402
from weft_cloud.storage import SqliteWalBackend  # noqa: E402


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


def _get(base: str, path: str, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


def _post_raw(base: str, path: str, body: dict, token: str | None) -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        finally:
            exc.close()


class ReloginRoomTestBase(unittest.TestCase):
    """Real HTTP service on a background thread, one fresh DB per test."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="weft-relogin-test-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        from http.server import ThreadingHTTPServer

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.service = WeftCloudService(SqliteWalBackend(self.db_path))
        _CloudHTTPHandler.service = self.service
        self.server = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self.server.start()
        _await_serving(self._httpd)

    def tearDown(self) -> None:
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

    def _signin(self, email: str, password: str) -> dict:
        status, body = _post(self.base, "/v1/auth/signin", {
            "email": email, "password": password,
        })
        self.assertEqual(status, 200, f"signin failed: {body}")
        return body

    def _create_room(self, token: str, cap: int = 8, **kwargs) -> dict:
        status, body = _post(self.base, "/v1/rooms/create", {"cap": cap, **kwargs}, token)
        self.assertEqual(status, 201, f"room create failed: {body}")
        return body

    def _join_room(self, token: str, room_id: str, link_token: str,
                   consent: bool = True) -> dict:
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room_id,
            "link_token": link_token,
            "consent": consent,
        }, token)
        self.assertEqual(status, 200, f"join failed: {body}")
        return body


class TestMembershipSurvivesRelogin(ReloginRoomTestBase):
    """Property 1 — the regression. Join with session #1, re-sign-in, operate with #2."""

    def test_owner_can_run_every_room_op_after_relogin(self) -> None:
        alice = self._signup("relogin-owner@example.com", "CorrectHorse!1")
        token1 = alice["session_token"]
        room = self._create_room(token1, cap=8)
        room_id = room["room_id"]
        account_id = alice["account_id"]

        # Re-sign-in: session #2, same account, different token.
        token2 = self._signin("relogin-owner@example.com", "CorrectHorse!1")["session_token"]
        self.assertNotEqual(token1, token2, "a fresh sign-in must mint a fresh session token")

        # Sanity: with the ORIGINAL token the room is reachable.
        s, _ = _post(self.base, "/v1/rooms/poll", {"room_id": room_id}, token1)
        self.assertEqual(s, 200)

        # Now the same human, with the NEW token, can run every room op.
        s, poll = _post(self.base, "/v1/rooms/poll", {"room_id": room_id, "after_seq": 0}, token2)
        self.assertEqual(s, 200, "re-login poll must not 404")
        self.assertEqual(poll["room_id"], room_id)

        s, info = _get(self.base, f"/v1/rooms/info?room_id={room_id}", token2)
        self.assertEqual(s, 200, "re-login info must not 404")
        self.assertEqual(info["owner_agent_id"], account_id)

        s, log = _post(self.base, "/v1/rooms/event_log", {"room_id": room_id}, token2)
        self.assertEqual(s, 200, "re-login event_log must not 404")
        self.assertGreaterEqual(len(log["events"]), 2)  # room.created + room.joined

        s, sent = _post(self.base, "/v1/rooms/send", {
            "room_id": room_id, "target_spec": "*", "payload": {"text": "after-relogin"},
        }, token2)
        self.assertEqual(s, 200, "re-login send must not 404")
        seq = sent["seq"]

        s, ack = _post(self.base, "/v1/rooms/ack", {"room_id": room_id, "seq": seq}, token2)
        self.assertEqual(s, 200, "re-login ack must not 404")
        self.assertEqual(ack["last_ack_seq"], seq)

        s, hb = _post(self.base, "/v1/rooms/heartbeat", {"room_id": room_id}, token2)
        self.assertEqual(s, 200, "re-login heartbeat must not 404")
        self.assertEqual(hb["status"], "active")

        # wait (short timeout, no new events -> empty result, NOT an error).
        s, waited = _post(self.base, "/v1/rooms/wait", {
            "room_id": room_id, "timeout_seconds": 1,
        }, token2)
        self.assertEqual(s, 200, "re-login wait must not 404")

        s, left = _post(self.base, "/v1/rooms/leave", {"room_id": room_id}, token2)
        self.assertEqual(s, 200, "re-login leave must not 404")
        self.assertEqual(left["status"], "left")

    def test_joined_member_can_operate_after_relogin(self) -> None:
        owner = self._signup("relogin-owner2@example.com", "CorrectHorse!1")
        room = self._create_room(owner["session_token"], cap=8)
        peer = self._signup("relogin-peer@example.com", "AgentPass!1")
        token1 = peer["session_token"]
        self._join_room(token1, room["room_id"], room["link_token"])

        # Re-sign-in -> session #2.
        token2 = self._signin("relogin-peer@example.com", "AgentPass!1")["session_token"]
        self.assertNotEqual(token1, token2)

        s, poll = _post(self.base, "/v1/rooms/poll", {"room_id": room["room_id"]}, token2)
        self.assertEqual(s, 200, "re-login member poll must not 404")
        self.assertEqual(poll["room_id"], room["room_id"])

        # Re-join with session #2 is idempotent (same account).
        rejoined = self._join_room(token2, room["room_id"], room["link_token"])
        self.assertEqual(rejoined["status"], "active")

        s, sent = _post(self.base, "/v1/rooms/send", {
            "room_id": room["room_id"], "target_spec": "*", "payload": {"text": "hi"},
        }, token2)
        self.assertEqual(s, 200, "re-login member send must not 404")

    def test_relogin_does_not_duplicate_membership(self) -> None:
        owner = self._signup("relogin-owner3@example.com", "CorrectHorse!1")
        room = self._create_room(owner["session_token"], cap=8)
        peer = self._signup("relogin-peer3@example.com", "AgentPass!1")
        token1 = peer["session_token"]
        self._join_room(token1, room["room_id"], room["link_token"])

        token2 = self._signin("relogin-peer3@example.com", "AgentPass!1")["session_token"]
        self.assertNotEqual(token1, token2)

        s, info = _get(self.base, f"/v1/rooms/info?room_id={room['room_id']}", token2)
        self.assertEqual(s, 200)
        self.assertEqual(info["member_count"], 2, "re-login must not add a second seat")


class TestIdentityFromSessionNeverBody(ReloginRoomTestBase):
    """Property 2 — impersonation stays blocked because identity is never a body argument."""

    def test_body_identity_arguments_are_rejected_outright(self) -> None:
        alice = self._signup("imp-owner@example.com", "CorrectHorse!1")
        room = self._create_room(alice["session_token"], cap=8, owner_agent_id=alice["account_id"])
        bob = self._signup("imp-attacker@example.com", "CorrectHorse!1")

        # Bob (tenant B) stuffs alice's account_id into every identity argument.
        attempts = [
            ("POST", "/v1/rooms/poll", {"room_id": room["room_id"], "agent_id": alice["account_id"]}),
            ("POST", "/v1/rooms/send", {
                "room_id": room["room_id"], "sender_agent_id": alice["account_id"],
                "target_spec": "*", "payload": {"text": "forged"}}),
            ("POST", "/v1/rooms/close", {
                "room_id": room["room_id"], "caller_agent_id": alice["account_id"]}),
            ("POST", "/v1/rooms/leave", {
                "room_id": room["room_id"], "agent_id": alice["account_id"]}),
            ("POST", "/v1/rooms/event_log", {
                "room_id": room["room_id"], "agent_id": alice["account_id"]}),
            ("POST", "/v1/rooms/heartbeat", {
                "room_id": room["room_id"], "agent_id": alice["account_id"]}),
            ("POST", "/v1/rooms/ack", {
                "room_id": room["room_id"], "agent_id": alice["account_id"], "seq": 1}),
            ("POST", "/v1/rooms/join", {
                "room_id": room["room_id"], "link_token": room["link_token"],
                "agent_id": alice["account_id"], "consent": True}),
            ("POST", "/v1/rooms/revoke_link", {
                "room_id": room["room_id"], "owner_agent_id": alice["account_id"],
                "link_id": room["link_id"]}),
        ]
        for method, path, body in attempts:
            with self.subTest(path=path):
                s, resp = _post(self.base, path, body, bob["session_token"])
                self.assertEqual(s, 400, f"{path}: body identity must be refused")
                self.assertEqual(resp["error"]["code"], "invalid_argument")
                self.assertIn("not accepted", resp["error"]["message"])

        # GET /v1/rooms/info with a query-param agent_id is equally ignored:
        # the caller resolves as ITS OWN account (bob, not a member -> 404).
        s, _ = _get(self.base,
                    f"/v1/rooms/info?room_id={room['room_id']}&agent_id={alice['account_id']}",
                    bob["session_token"])
        self.assertEqual(s, 404, "query-param agent_id must be ignored, not honored")

    def test_body_identity_rejected_for_real_and_fabricated_room_identically(self) -> None:
        alice = self._signup("imp2-owner@example.com", "CorrectHorse!1")
        room = self._create_room(alice["session_token"], cap=8)
        bob = self._signup("imp2-attacker@example.com", "CorrectHorse!1")
        fake_room = "room_" + "f" * 32

        for path, body in [
            ("/v1/rooms/poll", {"room_id": room["room_id"], "agent_id": alice["account_id"]}),
            ("/v1/rooms/send", {
                "room_id": room["room_id"], "sender_agent_id": alice["account_id"],
                "target_spec": "*", "payload": {"text": "forged"}}),
            ("/v1/rooms/close", {"room_id": room["room_id"], "caller_agent_id": alice["account_id"]}),
            ("/v1/rooms/event_log", {"room_id": room["room_id"], "agent_id": alice["account_id"]}),
        ]:
            with self.subTest(path=path):
                fake_body = dict(body)
                fake_body["room_id"] = fake_room
                s_real, b_real = _post(self.base, path, body, bob["session_token"])
                s_fake, b_fake = _post(self.base, path, fake_body, bob["session_token"])
                # The refusal is a property of the SESSION identity, decided
                # before any room lookup — real and fabricated are identical.
                self.assertEqual(s_real, s_fake)
                self.assertEqual(b_real, b_fake)
                self.assertEqual(s_real, 400)

    def test_self_referential_identity_arguments_are_also_rejected(self) -> None:
        alice = self._signup("imp3-owner@example.com", "CorrectHorse!1")
        room = self._create_room(alice["session_token"], cap=8)
        # Even the OWN account_id is not accepted as a body identity argument:
        # identity is derived from the session, never supplied.
        s, resp = _post(self.base, "/v1/rooms/poll", {
            "room_id": room["room_id"], "agent_id": alice["account_id"],
        }, alice["session_token"])
        self.assertEqual(s, 400)
        self.assertEqual(resp["error"]["code"], "invalid_argument")

    def test_hosted_mcp_rejects_unknown_tool_arguments(self) -> None:
        alice = self._signup("unknown-arg-owner@example.com", "CorrectHorse!1")
        room = self._create_room(alice["session_token"], cap=8)
        # The advertised hosted schemas set additionalProperties=false; an
        # ignored extra field would make the wire contract weaker than declared.
        s, body = _post_raw(self.base, "/mcp", {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "room_info",
                "arguments": {"room_id": room["room_id"], "unexpected": "ignored?"},
            },
        }, alice["session_token"])
        self.assertEqual(s, 200)
        response = json.loads(body.decode("utf-8"))
        result = response["result"]
        self.assertTrue(result["isError"])
        error = json.loads(result["content"][0]["text"])["error"]
        self.assertEqual(error["code"], "invalid_argument")


class TestNoRoomExistenceOracle(ReloginRoomTestBase):
    """Property 3 — a non-member's refusal is identical for a real and a fabricated room."""

    def test_non_member_refusal_byte_identical_real_vs_fabricated(self) -> None:
        alice = self._signup("nooracle-owner@example.com", "CorrectHorse!1")
        room = self._create_room(alice["session_token"], cap=8)
        room_id = room["room_id"]
        outsider = self._signup("nooracle-outsider@example.com", "CorrectHorse!1")
        fake_room_id = "room_" + "f" * 32

        attempts = [
            ("POST", "/v1/rooms/poll", {"room_id": room_id}),
            ("POST", "/v1/rooms/send", {
                "room_id": room_id, "target_spec": "*", "payload": {"text": "x"}}),
            ("POST", "/v1/rooms/close", {"room_id": room_id}),
            ("POST", "/v1/rooms/leave", {"room_id": room_id}),
            ("POST", "/v1/rooms/event_log", {"room_id": room_id}),
            ("POST", "/v1/rooms/heartbeat", {"room_id": room_id}),
            ("POST", "/v1/rooms/ack", {"room_id": room_id, "seq": 1}),
            ("POST", "/v1/rooms/wait", {"room_id": room_id, "timeout_seconds": 1}),
            ("POST", "/v1/rooms/groups", {
                "room_id": room_id, "group_name": "g", "action": "list"}),
            ("POST", "/v1/rooms/revoke_link", {
                "room_id": room_id, "link_id": room["link_id"]}),
        ]
        for method, path, body in attempts:
            with self.subTest(path=path):
                fake_body = dict(body)
                fake_body["room_id"] = fake_room_id
                s_real, b_real = _post(self.base, path, body, outsider["session_token"])
                s_fake, b_fake = _post(self.base, path, fake_body, outsider["session_token"])
                # Equal to EACH OTHER — not to a literal — so a re-split of the
                # codes into two new distinct values still fails this suite.
                self.assertEqual(s_real, s_fake,
                                 f"{path}: non-member real vs fake must not differ")
                self.assertEqual(b_real, b_fake,
                                 f"{path}: non-member real vs fake must be byte-identical")
                self.assertEqual(s_real, HTTPStatus.NOT_FOUND)
                self.assertEqual(b_real["error"]["code"], "room_not_found")

        # GET info is covered too.
        s_real, b_real = _get(self.base, f"/v1/rooms/info?room_id={room_id}",
                              outsider["session_token"])
        s_fake, b_fake = _get(self.base, f"/v1/rooms/info?room_id={fake_room_id}",
                              outsider["session_token"])
        self.assertEqual(s_real, s_fake)
        self.assertEqual(b_real, b_fake)
        self.assertEqual(s_real, HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
