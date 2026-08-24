"""REST /v1 parity audit — regression contract for the REST-vs-hosted-MCP surface.

The hosted service exposes ONE room store behind TWO surfaces:

  - ``POST /mcp`` — the JSON-RPC surface (``src/weft_cloud/mcp.py``),
    exposing exactly 14 room tools: room_create, room_list, room_join, room_send,
    room_receipts, room_poll, room_wait, room_info, room_ack, room_heartbeat,
    room_leave, room_remove_member, room_close, room_event_log.
  - ``/v1/*`` — the REST surface (``src/weft_cloud/service.py``).

Both must offer the same product operations with the same machine-readable
error codes, and neither may turn unvalidated client input into a 500.

Audit findings encoded here (each RED test fails against the current
implementation and is the regression guard for the fix):

  R1  POST /v1/rooms/receipts does not exist — room_receipts has no REST
      equivalent (currently 404 not_found).
  R2  POST /v1/rooms/remove_member does not exist — room_remove_member has no
      REST equivalent (currently 404 not_found).
  R3  POST /v1/rooms/poll with a non-integer after_seq returns 500
      internal_error instead of a 400 invalid_argument/invalid_cursor.
  R4  POST /v1/rooms/wait with a non-integer after_seq returns 500
      internal_error instead of a 400 invalid_argument/invalid_cursor.
  R5  POST /v1/rooms/ack with a non-integer seq returns 500 internal_error
      instead of a 400 invalid_argument.
  R6  POST /v1/rooms/ack with a negative seq is silently accepted (200)
      instead of being refused with 400 invalid_cursor (the same refusal
      poll applies to a negative after_seq).
  R7  POST /v1/rooms/poll with a non-integer limit returns 500
      internal_error instead of a 400 invalid_argument.

The GREEN tests in this file pin the parity that already holds (same error
code/name as the MCP surface for the same failure) so a fix for R1-R7 can
never regress it.

Authoritative specs: docs/HOSTED_MCP_DESIGN.md, docs/ROOMS_DESIGN.md §6.
"""

from __future__ import annotations
from tests._server_readiness import await_serving as _await_serving

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler  # noqa: E402
from weft_cloud.storage import SqliteWalBackend  # noqa: E402

# Generous test-tuned auth limits: this file signs up several fresh accounts
# per run and must never trip the (production-tuned) per-IP auth limiter.
_TEST_AUTH_LIMITS = {
    "signup": {"ip": 1000, "email": 1000, "window_seconds": 900},
    "signin": {"ip": 1000, "email": 1000, "window_seconds": 900},
    "reset_request": {"ip": 1000, "email": 1000, "window_seconds": 900},
}

PASSWORD = "ParityPass!1"


class _ParityHarness:
    """One real HTTP service per test class; torn down even on failure."""

    def __init__(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="v1-parity-")
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self.service = WeftCloudService(
            SqliteWalBackend(str(Path(self._tmp) / "parity.db")),
            origin=self.base,
            auth_rate_limits=_TEST_AUTH_LIMITS,
        )
        _CloudHTTPHandler.service = self.service
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        _await_serving(self._httpd)
        self._counter = 0

    def close(self) -> None:
        try:
            self._httpd.shutdown()
        finally:
            self._httpd.server_close()
        try:
            self.service.backend.close()
        except Exception:
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _request(self, method: str, path: str, body: dict | None = None,
                 token: str | None = None, raw: bytes | None = None,
                 query: str = "") -> tuple[int, dict, dict]:
        data = raw if raw is not None else (
            json.dumps(body).encode("utf-8") if body is not None else b""
        )
        req = urllib.request.Request(self.base + path + query, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                return resp.status, (json.loads(payload.decode("utf-8")) if payload else {}), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read()
                parsed = json.loads(payload.decode("utf-8")) if payload else {}
                return exc.code, parsed, dict(exc.headers)
            finally:
                exc.close()

    def post(self, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
        return self._request("POST", path, body=body, token=token)[:2]

    def post_raw(self, path: str, raw: bytes, token: str | None = None) -> tuple[int, dict]:
        return self._request("POST", path, raw=raw, token=token)[:2]

    def get(self, path: str, token: str | None = None, query: str = "") -> tuple[int, dict]:
        return self._request("GET", path, token=token, query=query)[:2]

    def signup(self) -> dict:
        self._counter += 1
        email = f"parity{time.time_ns()}-{self._counter}@example.com"
        status, resp = self.post("/v1/auth/signup", {"email": email, "password": PASSWORD})
        assert status == HTTPStatus.CREATED, f"signup failed: {resp}"
        return resp

    def create_room(self, token: str, **overrides) -> dict:
        body = {"cap": 10, **overrides}
        status, resp = self.post("/v1/rooms/create", body, token=token)
        assert status == HTTPStatus.CREATED, f"create room failed: {status} {resp}"
        return resp

    def join(self, token: str, room_id: str, link_token: str,
             consent: object = True) -> tuple[int, dict]:
        return self.post("/v1/rooms/join", {
            "room_id": room_id,
            "link_token": link_token,
            "consent": consent,
            "capabilities": [],
        }, token=token)

    def error_code(self, resp: dict) -> str | None:
        return (resp.get("error") or {}).get("code")


# ---------------------------------------------------------------------------
# RED — route/feature parity gaps (MCP tool with no REST equivalent)
# ---------------------------------------------------------------------------


class TestRestRouteParityMissingEndpoints(unittest.TestCase):
    """MCP exposes room_receipts and room_remove_member; /v1 does not.

    RED: the endpoints currently return 404 not_found, so an agent using only
    the REST surface cannot query delivery/read state for its own sends and an
    owner cannot free a member's seat. The fix adds /v1/rooms/receipts and
    /v1/rooms/remove_member wired to CloudRoomService.receipts /
    .remove_member with the same tenant resolution as every other room route.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _ParityHarness()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.close()

    def test_room_receipts_has_rest_equivalent(self) -> None:
        owner = self.h.signup()
        room = self.h.create_room(owner["session_token"])
        agent = self.h.signup()
        status, joined = self.h.join(agent["session_token"], room["room_id"], room["link_token"])
        self.assertEqual(status, HTTPStatus.OK, joined)
        status, sent = self.h.post("/v1/rooms/send", {
            "room_id": room["room_id"],
            "target_spec": agent["account_id"],
            "payload": {"kind": "parity"},
        }, token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.OK, sent)
        entry_ids = [r["entry_id"] for r in sent["receipts"]]
        self.assertTrue(entry_ids)

        # RED: 404 not_found today. Parity requires the sender to query its
        # own envelopes with the same result shape the MCP tool returns.
        status, receipts = self.h.post("/v1/rooms/receipts", {
            "room_id": room["room_id"],
            "entry_ids": entry_ids,
        }, token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.OK,
                         f"/v1/rooms/receipts missing: {status} {receipts}")
        self.assertIn("receipts", receipts)
        got = {r["entry_id"]: r for r in receipts["receipts"]}
        for entry_id in entry_ids:
            self.assertIn(entry_id, got)
            self.assertEqual(got[entry_id]["read_status"], "queued")
        # Unknown / non-owned entry ids must not reveal outbox state.
        status, unknown = self.h.post("/v1/rooms/receipts", {
            "room_id": room["room_id"],
            "entry_ids": ["oev_deadbeef"],
        }, token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(unknown["receipts"][0]["status"], "not_found")

    def test_room_remove_member_has_rest_equivalent(self) -> None:
        owner = self.h.signup()
        room = self.h.create_room(owner["session_token"])
        agent = self.h.signup()
        status, _ = self.h.join(agent["session_token"], room["room_id"], room["link_token"])
        self.assertEqual(status, HTTPStatus.OK)

        # RED: 404 not_found today. The owner must be able to free the seat
        # over REST exactly as over MCP.
        status, removed = self.h.post("/v1/rooms/remove_member", {
            "room_id": room["room_id"],
            "member_id": agent["account_id"],
        }, token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.OK,
                         f"/v1/rooms/remove_member missing: {status} {removed}")
        self.assertEqual(removed.get("status"), "left")
        # The removed member is refused on its very next request.
        status, poll = self.h.post("/v1/rooms/poll", {
            "room_id": room["room_id"],
        }, token=agent["session_token"])
        self.assertEqual(status, HTTPStatus.NOT_FOUND, poll)
        self.assertEqual(self.h.error_code(poll), "room_not_found")

    def test_rest_routes_cover_the_hosted_mcp_tool_set(self) -> None:
        """Every hosted MCP tool has a corresponding REST operation."""
        from weft_cloud.mcp import HOSTED_TOOL_NAMES

        rest_routes = {
            "room_create": "/v1/rooms/create",
            "room_list": "/v1/rooms",
            "room_join": "/v1/rooms/join",
            "room_send": "/v1/rooms/send",
            "room_receipts": "/v1/rooms/receipts",
            "room_poll": "/v1/rooms/poll",
            "room_wait": "/v1/rooms/wait",
            "room_info": "/v1/rooms/info",
            "room_ack": "/v1/rooms/ack",
            "room_heartbeat": "/v1/rooms/heartbeat",
            "room_leave": "/v1/rooms/leave",
            "room_remove_member": "/v1/rooms/remove_member",
            "room_close": "/v1/rooms/close",
            "room_event_log": "/v1/rooms/event_log",
        }
        self.assertEqual(set(rest_routes), set(HOSTED_TOOL_NAMES),
                         "every hosted MCP room tool must map to a REST route")
        # Probe each route exists. A fabricated room_id on a HANDLED route
        # yields the domain 404 code `room_not_found` (or a 400 for a missing
        # argument); a route the router does not know at all yields its own
        # `not_found`. The discriminator is the error CODE, not the status.
        owner = self.h.signup()
        for tool, route in rest_routes.items():
            if tool == "room_list":
                status, body = self.h.get(route, token=owner["session_token"])
            elif tool == "room_info":
                status, body = self.h.get(route, token=owner["session_token"],
                                          query="?room_id=room_deadbeef")
            else:
                status, body = self.h.post(route, {"room_id": "room_deadbeef"},
                                           token=owner["session_token"])
            self.assertNotEqual(
                self.h.error_code(body), "not_found",
                f"REST route {route} (tool {tool}) is missing: {status} {body}",
            )


# ---------------------------------------------------------------------------
# RED — input validation: unvalidated input must not become a 500
# ---------------------------------------------------------------------------


class TestRestInputValidationNo500s(unittest.TestCase):
    """Unvalidated integer coercions currently 500 on the REST surface.

    RED: after_seq / seq / limit are coerced with a bare int() inside
    CloudRoomService.poll/ack, so a non-integer raises ValueError -> the
    handler's generic except -> 500 internal_error. A malformed argument is
    the CALLER's error and must come back as a 400 invalid_argument (or
    invalid_cursor), identical in code to what a schema-validating MCP client
    would receive.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _ParityHarness()
        cls.owner = cls.h.signup()
        cls.room = cls.h.create_room(cls.owner["session_token"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.close()

    def _assert_clean_400(self, resp: dict, status: int, allowed: set[str]) -> None:
        self.assertNotEqual(status, 500,
                            f"unvalidated input produced a 500: {resp}")
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertIn(self.h.error_code(resp), allowed)

    def test_poll_rejects_non_integer_after_seq(self) -> None:
        status, resp = self.h.post("/v1/rooms/poll", {
            "room_id": self.room["room_id"], "after_seq": "abc",
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status, {"invalid_argument", "invalid_cursor"})

    def test_wait_rejects_non_integer_after_seq(self) -> None:
        status, resp = self.h.post("/v1/rooms/wait", {
            "room_id": self.room["room_id"], "after_seq": "abc", "timeout_seconds": 1,
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status, {"invalid_argument", "invalid_cursor"})

    def test_ack_rejects_non_integer_seq(self) -> None:
        status, resp = self.h.post("/v1/rooms/ack", {
            "room_id": self.room["room_id"], "seq": "abc",
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status, {"invalid_argument"})

    def test_ack_rejects_negative_seq(self) -> None:
        # poll refuses a negative after_seq with invalid_cursor; ack must be
        # symmetric. Today a negative seq is silently accepted (200).
        status, resp = self.h.post("/v1/rooms/ack", {
            "room_id": self.room["room_id"], "seq": -5,
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status, {"invalid_cursor", "invalid_argument"})

    def test_poll_rejects_non_integer_limit(self) -> None:
        status, resp = self.h.post("/v1/rooms/poll", {
            "room_id": self.room["room_id"], "limit": "abc",
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status, {"invalid_argument"})


# ---------------------------------------------------------------------------
# GREEN — error-code parity pins (same code/name as the MCP surface)
# ---------------------------------------------------------------------------


class TestRestMcpErrorCodeParity(unittest.TestCase):
    """The same failure must carry the same machine-readable code on both
    surfaces: recipient_not_found, invalid_cursor, consent_required,
    quota_exceeded (with the caller's own limit block), room_closed,
    room_expired."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _ParityHarness()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.close()

    def test_recipient_not_found_422_matches_mcp(self) -> None:
        owner = self.h.signup()
        room = self.h.create_room(owner["session_token"])
        status, resp = self.h.post("/v1/rooms/send", {
            "room_id": room["room_id"],
            "target_spec": "acct_deadbeef",
            "payload": {"x": 1},
        }, token=owner["session_token"])
        self.assertEqual(status, 422, resp)
        self.assertEqual(self.h.error_code(resp), "recipient_not_found")

    def test_invalid_cursor_400_matches_mcp(self) -> None:
        owner = self.h.signup()
        room = self.h.create_room(owner["session_token"])
        status, resp = self.h.post("/v1/rooms/poll", {
            "room_id": room["room_id"], "after_seq": -1,
        }, token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertEqual(self.h.error_code(resp), "invalid_cursor")

    def test_consent_required_matches_mcp(self) -> None:
        owner = self.h.signup()
        room = self.h.create_room(owner["session_token"])
        agent = self.h.signup()
        for consent in ("true", 1, None):
            status, resp = self.h.join(agent["session_token"], room["room_id"],
                                       room["link_token"], consent=consent)
            self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
            self.assertEqual(self.h.error_code(resp), "consent_required")

    def test_quota_exceeded_409_with_limit_block_matches_mcp(self) -> None:
        owner = self.h.signup()
        status, resp = self.h.post("/v1/rooms/create", {"cap": 16},
                                   token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.CONFLICT, resp)
        self.assertEqual(self.h.error_code(resp), "quota_exceeded")
        limit = (resp.get("error") or {}).get("limit") or {}
        self.assertEqual(limit.get("name"), "max_members_per_room")
        self.assertEqual(limit.get("value"), 15)
        self.assertEqual(limit.get("plan"), "free")

    def test_room_closed_409_matches_mcp(self) -> None:
        owner = self.h.signup()
        room = self.h.create_room(owner["session_token"])
        status, closed = self.h.post("/v1/rooms/close", {"room_id": room["room_id"]},
                                     token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.OK, closed)
        status, resp = self.h.post("/v1/rooms/send", {
            "room_id": room["room_id"],
            "target_spec": "*",
            "payload": {"x": 1},
        }, token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.CONFLICT, resp)
        self.assertEqual(self.h.error_code(resp), "room_closed")

    def test_room_expired_410_matches_mcp(self) -> None:
        owner = self.h.signup()
        room = self.h.create_room(owner["session_token"], ttl_seconds=1)
        agent = self.h.signup()
        time.sleep(1.2)
        status, resp = self.h.join(agent["session_token"], room["room_id"],
                                   room["link_token"])
        self.assertEqual(status, 410, resp)
        self.assertEqual(self.h.error_code(resp), "room_expired")


# ---------------------------------------------------------------------------
# GREEN — create/body validation pins (cap bounds, ttl, payload size, JSON)
# ---------------------------------------------------------------------------


class TestRestCreateAndBodyValidation(unittest.TestCase):
    """The create/body validations the audit checklist names, pinned as green:
    cap type + bounds (including the plan max), ttl_seconds, the 1 MiB read
    cap, and JSON-body shape. None of these may regress into a 500."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _ParityHarness()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.close()

    def test_cap_must_be_integer_at_least_2(self) -> None:
        owner = self.h.signup()
        for bad in (1, 0, -3, "8", None, 2.5, True):
            status, resp = self.h.post("/v1/rooms/create", {"cap": bad},
                                       token=owner["session_token"])
            self.assertNotEqual(status, 500, f"cap={bad!r} produced a 500")
            self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
            self.assertEqual(self.h.error_code(resp), "invalid_argument")

    def test_cap_above_plan_max_refused_not_clamped(self) -> None:
        owner = self.h.signup()
        status, resp = self.h.post("/v1/rooms/create", {"cap": 15},
                                   token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.CREATED, resp)
        status, resp = self.h.post("/v1/rooms/create", {"cap": 16},
                                   token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.CONFLICT, resp)
        self.assertEqual(self.h.error_code(resp), "quota_exceeded")

    def test_ttl_seconds_validated(self) -> None:
        owner = self.h.signup()
        for bad in (-5, 0, "abc", None):
            status, resp = self.h.post("/v1/rooms/create", {"ttl_seconds": bad},
                                       token=owner["session_token"])
            self.assertNotEqual(status, 500, f"ttl_seconds={bad!r} produced a 500")
            self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
            self.assertEqual(self.h.error_code(resp), "invalid_argument")

    def test_name_validated(self) -> None:
        owner = self.h.signup()
        status, resp = self.h.post("/v1/rooms/create", {"name": 123},
                                   token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertEqual(self.h.error_code(resp), "invalid_argument")
        status, resp = self.h.post("/v1/rooms/create", {"name": "x" * 161},
                                   token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertEqual(self.h.error_code(resp), "invalid_argument")

    def test_body_size_cap_enforced(self) -> None:
        owner = self.h.signup()
        big = json.dumps({"room_id": "room_deadbeef",
                          "payload": "x" * (2 * 1024 * 1024)}).encode("utf-8")
        status, resp = self.h.post_raw("/v1/rooms/send", big,
                                       token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertEqual(self.h.error_code(resp), "invalid_body")

    def test_malformed_json_and_non_object_bodies_refused_cleanly(self) -> None:
        owner = self.h.signup()
        status, resp = self.h.post_raw("/v1/rooms/poll", b"{not json",
                                       token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertEqual(self.h.error_code(resp), "invalid_json")
        status, resp = self.h.post_raw("/v1/rooms/poll", b"[1,2,3]",
                                       token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertEqual(self.h.error_code(resp), "invalid_body")
        status, resp = self.h.post_raw("/v1/rooms/poll", b"",
                                       token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertEqual(self.h.error_code(resp), "invalid_body")

    def test_identity_arguments_rejected(self) -> None:
        owner = self.h.signup()
        room = self.h.create_room(owner["session_token"])
        status, resp = self.h.post("/v1/rooms/create", {
            "cap": 10, "agent_id": "acct_deadbeef",
        }, token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertEqual(self.h.error_code(resp), "invalid_argument")
        status, resp = self.h.post("/v1/rooms/create", {
            "cap": 10, "owner_agent_id": "acct_deadbeef",
        }, token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertEqual(self.h.error_code(resp), "invalid_argument")
        status, resp = self.h.post("/v1/rooms/poll", {
            "room_id": room["room_id"], "sender_agent_id": "acct_deadbeef",
        }, token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertEqual(self.h.error_code(resp), "invalid_argument")


if __name__ == "__main__":
    unittest.main()
