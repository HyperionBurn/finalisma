"""Input hardening regression guards — remediation for SECURITY_AUDIT_2026-08-15.

Guards the fixes for the audit's input-validation findings on the hosted
surfaces. Every test here was RED before the fix (observed 500 internal_error
on REST, internal_error tool result on MCP) and is the regression guard for it:

  H1  MEDIUM-1: an oversized ``message_kinds`` poll filter (past the SQLite
      bind limit) 500s /v1/rooms/poll and /v1/rooms/wait. Must be a 400
      invalid_argument, never a 500. (rooms.py _validate_message_kinds cap.)
  H2  LOW-1: a non-string container ``room_id`` (list/dict) 500s every /v1
      room route. Must be a 400 invalid_argument, never a 500. (service.py
      _room_tenant / rooms.py _validate_room_id.)
  H3  LOW-2: a non-string ``member_id`` on /v1/rooms/remove_member 500s. Must
      be a 400 invalid_argument. (rooms.py remove_member type check.)
  H4  LOW-3: an int/bool/float ``target_spec`` on room_send 500s (list(...)
      TypeError). Must be a 400 invalid_argument. (rooms.py
      _normalize_target_spec.)
  H5  LOW-4: attacker-controlled strings were reflected verbatim into
      responses (receipts entry_id echo). Bounded now with type + length
      caps that fit the existing invalid_argument vocabulary.

The MCP parity pins prove the SAME failure carries the SAME machine-readable
code on /mcp as on /v1 (invalid_argument), exactly like the 1e0aa5a cursor
fixes. Tests drive the real HTTP service against real SQLite-WAL storage —
no mocks for the storage layer.

Authoritative context: docs/SECURITY_AUDIT_2026-08-15.md
(findings MEDIUM-1, LOW-1..4).
"""

from __future__ import annotations

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

PASSWORD = "HardeningPass!1"

# 40 000 distinct valid slugs: past SQLite's SQLITE_MAX_VARIABLE_NUMBER
# (32 766) so the old code raised OperationalError -> HTTP 500. Each slug is
# 11 bytes, so the JSON body stays ~560 KB — under the 1 MiB _read_body cap.
_OVERSIZED_KINDS = [f"kind{i:05d}" for i in range(40000)]


class _HardeningHarness:
    """One real HTTP service per test class; torn down even on failure."""

    def __init__(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="input-hardening-")
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self.service = WeftCloudService(
            SqliteWalBackend(str(Path(self._tmp) / "hardening.db")),
            origin=self.base,
            auth_rate_limits=_TEST_AUTH_LIMITS,
        )
        _CloudHTTPHandler.service = self.service
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
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
                 token: str | None = None) -> tuple[int, dict, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        req = urllib.request.Request(self.base + path, data=data, method=method)
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

    def signup(self) -> dict:
        self._counter += 1
        email = f"hardening{time.time_ns()}-{self._counter}@example.com"
        status, resp = self.post("/v1/auth/signup", {"email": email, "password": PASSWORD})
        assert status == HTTPStatus.CREATED, f"signup failed: {resp}"
        return resp

    def create_room(self, token: str, **overrides) -> dict:
        body = {"cap": 10, **overrides}
        status, resp = self.post("/v1/rooms/create", body, token=token)
        assert status == HTTPStatus.CREATED, f"create room failed: {status} {resp}"
        return resp

    def join(self, token: str, room_id: str, link_token: str) -> tuple[int, dict]:
        return self.post("/v1/rooms/join", {
            "room_id": room_id,
            "link_token": link_token,
            "consent": True,
            "capabilities": [],
        }, token=token)

    def mcp_call(self, token: str, name: str, args: dict) -> dict:
        """POST /mcp tools/call; returns {"isError": bool, "error": {...}}."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": args}}
        status, payload = self.post("/mcp", body, token=token)
        assert status == HTTPStatus.OK, f"tools/call {name} HTTP {status}: {payload}"
        result = (payload or {}).get("result") or {}
        if result.get("isError"):
            content = result.get("content") or []
            text = content[0].get("text", "") if content else ""
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"error": {"code": "unparseable", "message": text}}
            return {"isError": True, "error": parsed.get("error") or {"code": "missing"}}
        return {"isError": False, "result": result.get("structuredContent")}

    def error_code(self, resp: dict) -> str | None:
        return (resp.get("error") or {}).get("code")


class _HardeningTestCase(unittest.TestCase):
    """Base: one harness + one owner + one room per test class."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _HardeningHarness()
        cls.owner = cls.h.signup()
        cls.room = cls.h.create_room(cls.owner["session_token"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.h.close()

    def _assert_clean_400(self, resp: dict, status: int) -> None:
        self.assertNotEqual(status, 500, f"unvalidated input produced a 500: {resp}")
        self.assertEqual(status, HTTPStatus.BAD_REQUEST, resp)
        self.assertEqual(self.h.error_code(resp), "invalid_argument")


# ---------------------------------------------------------------------------
# H1 — MEDIUM-1: oversized message_kinds must be a 400, never a 500
# ---------------------------------------------------------------------------


class TestOversizedMessageKindsRefused(_HardeningTestCase):
    """40 000 kinds overflowed SQLite's bind limit -> 500 on poll and wait.

    RED: both /v1/rooms/poll and /v1/rooms/wait returned 500 internal_error
    with the oversized filter. The fix caps the list in
    ``_validate_message_kinds`` and returns 400 invalid_argument beyond it.
    """

    def test_poll_rejects_oversized_message_kinds_with_400(self) -> None:
        status, resp = self.h.post("/v1/rooms/poll", {
            "room_id": self.room["room_id"],
            "message_kinds": _OVERSIZED_KINDS,
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status)

    def test_wait_rejects_oversized_message_kinds_with_400(self) -> None:
        status, resp = self.h.post("/v1/rooms/wait", {
            "room_id": self.room["room_id"],
            "timeout_seconds": 1,
            "message_kinds": _OVERSIZED_KINDS,
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status)

    def test_mcp_poll_rejects_oversized_message_kinds_with_invalid_argument(self) -> None:
        resp = self.h.mcp_call(self.owner["session_token"], "room_poll", {
            "room_id": self.room["room_id"],
            "message_kinds": _OVERSIZED_KINDS,
        })
        self.assertTrue(resp["isError"], f"room_poll unexpectedly succeeded: {resp}")
        self.assertEqual(resp["error"].get("code"), "invalid_argument", resp)

    def test_poll_allows_64_message_kinds(self) -> None:
        """The cap must not over-restrict: 64 valid kinds still work."""
        status, resp = self.h.post("/v1/rooms/poll", {
            "room_id": self.room["room_id"],
            "message_kinds": [f"kind{i:02d}" for i in range(64)],
        }, token=self.owner["session_token"])
        self.assertEqual(status, HTTPStatus.OK, resp)
        self.assertIn("events", resp)

    def test_poll_rejects_65_message_kinds_with_400(self) -> None:
        status, resp = self.h.post("/v1/rooms/poll", {
            "room_id": self.room["room_id"],
            "message_kinds": [f"kind{i:02d}" for i in range(65)],
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status)


# ---------------------------------------------------------------------------
# H2 — LOW-1: non-string container room_id must be a 400, never a 500
# ---------------------------------------------------------------------------


class TestNonStringRoomIdRefused(_HardeningTestCase):
    """A list/dict room_id 500ed every /v1 room route (sqlite bind failure).

    RED: POST with ``room_id: ["x"]`` (and ``{"a": 1}``) returned 500
    internal_error on poll/wait/send/receipts/ack/remove_member/heartbeat/
    leave/close/event_log. The fix type-checks room_id before any DB call.
    """

    _ROUTES = [
        ("/v1/rooms/poll", {"room_id": ["x"]}),
        ("/v1/rooms/wait", {"room_id": ["x"], "timeout_seconds": 1}),
        ("/v1/rooms/send", {"room_id": ["x"], "target_spec": "*", "payload": {}}),
        ("/v1/rooms/receipts", {"room_id": ["x"], "entry_ids": ["oev_deadbeef"]}),
        ("/v1/rooms/ack", {"room_id": ["x"], "seq": 0}),
        ("/v1/rooms/remove_member", {"room_id": ["x"], "member_id": "acct_deadbeef"}),
        ("/v1/rooms/heartbeat", {"room_id": ["x"]}),
        ("/v1/rooms/leave", {"room_id": ["x"]}),
        ("/v1/rooms/close", {"room_id": ["x"]}),
        ("/v1/rooms/event_log", {"room_id": ["x"]}),
    ]

    def test_non_string_room_id_400_on_every_room_route(self) -> None:
        for path, body in self._ROUTES:
            for bad in (["x"], {"a": 1}):
                status, resp = self.h.post(path, {**body, "room_id": bad},
                                           token=self.owner["session_token"])
                self._assert_clean_400(resp, status)
                self.assertIsNone(resp.get("traceback"),
                                  f"{path} leaked internals: {resp}")

    def test_join_with_non_string_room_id_400(self) -> None:
        """Join resolves through _resolve_room_for_link — same guard needed."""
        for bad in (["x"], {"a": 1}):
            status, resp = self.h.post("/v1/rooms/join", {
                "room_id": bad,
                "link_token": "x" * 32,
                "consent": True,
                "capabilities": [],
            }, token=self.owner["session_token"])
            self._assert_clean_400(resp, status)

    def test_mcp_poll_with_list_room_id_is_invalid_argument(self) -> None:
        resp = self.h.mcp_call(self.owner["session_token"], "room_poll", {
            "room_id": ["x"],
        })
        self.assertTrue(resp["isError"], f"room_poll unexpectedly succeeded: {resp}")
        self.assertEqual(resp["error"].get("code"), "invalid_argument", resp)


# ---------------------------------------------------------------------------
# H3 — LOW-2: non-string member_id on remove_member must be a 400, never 500
# ---------------------------------------------------------------------------


class TestNonStringMemberIdRefused(_HardeningTestCase):
    """A list/dict member_id 500ed the owner-only remove_member route.

    RED: 500 internal_error on the sqlite bind of target_agent_id. The fix
    type-checks member_id inside remove_member and raises invalid_argument.
    """

    def test_remove_member_rejects_non_string_member_id_with_400(self) -> None:
        member = self.h.signup()
        status, joined = self.h.join(member["session_token"], self.room["room_id"],
                                     self.room["link_token"])
        self.assertEqual(status, HTTPStatus.OK, joined)
        for bad in (["x"], {"a": 1}, 42):
            status, resp = self.h.post("/v1/rooms/remove_member", {
                "room_id": self.room["room_id"],
                "member_id": bad,
            }, token=self.owner["session_token"])
            self._assert_clean_400(resp, status)

    def test_mcp_remove_member_with_list_member_id_is_invalid_argument(self) -> None:
        resp = self.h.mcp_call(self.owner["session_token"], "room_remove_member", {
            "room_id": self.room["room_id"],
            "member_id": ["x"],
        })
        self.assertTrue(resp["isError"], f"room_remove_member unexpectedly succeeded: {resp}")
        self.assertEqual(resp["error"].get("code"), "invalid_argument", resp)

    def test_remove_member_with_unknown_string_member_id_still_404(self) -> None:
        """The type guard must not shadow the existing member_not_found 404."""
        status, resp = self.h.post("/v1/rooms/remove_member", {
            "room_id": self.room["room_id"],
            "member_id": "acct_deadbeef",
        }, token=self.owner["session_token"])
        self.assertEqual(status, HTTPStatus.NOT_FOUND, resp)
        self.assertEqual(self.h.error_code(resp), "member_not_found")


# ---------------------------------------------------------------------------
# H4 — LOW-3: int/bool/float target_spec on room_send must be a 400
# ---------------------------------------------------------------------------


class TestBadTargetSpecRefused(_HardeningTestCase):
    """list(42)/list(True) raised TypeError -> 500 on room_send.

    RED: 500 internal_error for int/bool/float target_spec. The fix requires
    target_spec to be a string or a bounded list of strings, so a malformed
    spec is the caller's 400 invalid_argument on BOTH surfaces.
    """

    def test_send_rejects_int_bool_float_target_spec_with_400(self) -> None:
        for bad in (42, True, 3.5):
            status, resp = self.h.post("/v1/rooms/send", {
                "room_id": self.room["room_id"],
                "target_spec": bad,
                "payload": {"x": 1},
            }, token=self.owner["session_token"])
            self._assert_clean_400(resp, status)

    def test_send_rejects_dict_target_spec_with_400(self) -> None:
        status, resp = self.h.post("/v1/rooms/send", {
            "room_id": self.room["room_id"],
            "target_spec": {"a": 1},
            "payload": {"x": 1},
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status)

    def test_send_rejects_oversized_target_list_with_400(self) -> None:
        status, resp = self.h.post("/v1/rooms/send", {
            "room_id": self.room["room_id"],
            "target_spec": [f"ghost{i:03d}" for i in range(100)],
            "payload": {"x": 1},
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status)

    def test_send_rejects_overlong_target_entry_with_400(self) -> None:
        status, resp = self.h.post("/v1/rooms/send", {
            "room_id": self.room["room_id"],
            "target_spec": ["x" * 200],
            "payload": {"x": 1},
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status)

    def test_send_allows_string_and_list_of_strings(self) -> None:
        """The guard must not break the legitimate spec shapes."""
        for spec in ("*", [], ["*"]):
            status, resp = self.h.post("/v1/rooms/send", {
                "room_id": self.room["room_id"],
                "target_spec": spec,
                "payload": {"x": 1},
            }, token=self.owner["session_token"])
            self.assertEqual(status, HTTPStatus.OK, resp)

    def test_mcp_send_with_int_target_spec_is_invalid_argument(self) -> None:
        resp = self.h.mcp_call(self.owner["session_token"], "room_send", {
            "room_id": self.room["room_id"],
            "target_spec": 42,
            "payload": {"x": 1},
        })
        self.assertTrue(resp["isError"], f"room_send unexpectedly succeeded: {resp}")
        self.assertEqual(resp["error"].get("code"), "invalid_argument", resp)


# ---------------------------------------------------------------------------
# H5 — LOW-4: attacker-controlled reflection is now bounded (type + length)
# ---------------------------------------------------------------------------


class TestEntryIdReflectionBounded(_HardeningTestCase):
    """receipts echoed a caller's 200 KB entry_id back verbatim.

    RED: a 600-char entry_id returned 200 with the whole string echoed in the
    not_found entry. The fix caps entry_id length at 512 inside the existing
    invalid_argument vocabulary.
    """

    def test_receipts_rejects_overlong_entry_id_with_400(self) -> None:
        status, resp = self.h.post("/v1/rooms/receipts", {
            "room_id": self.room["room_id"],
            "entry_ids": ["oev_" + "x" * 600],
        }, token=self.owner["session_token"])
        self._assert_clean_400(resp, status)

    def test_receipts_accepts_512_char_entry_id_as_not_found(self) -> None:
        """The cap must not over-restrict: a 512-char id stays a not_found echo."""
        entry_id = "oev_" + "y" * 508
        status, resp = self.h.post("/v1/rooms/receipts", {
            "room_id": self.room["room_id"],
            "entry_ids": [entry_id],
        }, token=self.owner["session_token"])
        self.assertEqual(status, HTTPStatus.OK, resp)
        self.assertEqual(resp["receipts"][0]["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
