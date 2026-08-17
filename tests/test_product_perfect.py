"""Product-completeness fixes — the gaps that remained after the P0 sprint.

Three defects, each with a regression test that is RED before the fix:

1. ``room_leave`` is missing from the hosted MCP surface. The service has
   ``leave_room`` (and REST /v1/rooms/leave), but an MCP agent can join a room
   and has NO way to exit — the known-list item that outlived the sprint.
2. Room TTL is decorative. ``ttl_seconds`` is stored on the room row and
   enforced ONLY for the join link; the room itself never closes. A room
   created with ``ttl_seconds=60`` lives forever.
3. ``message_kind`` is dead surface on the hosted plane: the service accepts
   it, poll/wait filter on it, but the ``room_send`` tool schema never
   exposes it, so every send's message_kind is permanently NULL and the
   filter can never match anything.

Plus a proof test for the room message budget (60/min): crossing it must
produce a ``rate_limited`` refusal with ``retry_after`` — the security
surface measured earlier as "consistent with, not proven".

Tests drive the real hosted HTTP surface; no mocks for the SQLite layer.
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
from pathlib import Path

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
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


def _mcp(base: str, method: str, params: dict | None, token: str | None = None,
         request_id: int | None = 1, timeout: int = 10) -> tuple[int, dict | None]:
    body: dict = {"jsonrpc": "2.0", "method": method}
    if not request_id is None:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(base + "/mcp", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        payload = None
        try:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


def _tool_error_text(result: dict) -> dict:
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"code": "unparseable", "message": text}
    return parsed.get("error", {"code": "missing_error", "message": text})


class ProductPerfectTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from http.server import ThreadingHTTPServer

        cls.tmpdir = tempfile.mkdtemp(prefix="weft-perfect-test-")
        cls.db_path = str(Path(cls.tmpdir) / "test.db")
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        cls.port = cls._httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.service = WeftCloudService(SqliteWalBackend(cls.db_path))
        _CloudHTTPHandler.service = cls.service
        cls.server_thread = threading.Thread(
            target=cls._httpd.serve_forever, daemon=True,
        )
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._httpd is not None:
            try:
                cls._httpd.shutdown()
            finally:
                cls._httpd.server_close()
        try:
            cls.service.backend.close()
        except Exception:
            pass
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _signup(self, email: str, tenant_id: str | None = None) -> dict:
        body: dict = {"email": email, "password": "password-123"}
        if tenant_id is not None:
            body["tenant_id"] = tenant_id
        status, resp = _post(self.base, "/v1/auth/signup", body)
        self.assertEqual(status, HTTPStatus.CREATED, f"signup failed: {resp}")
        return resp

    def _mcp_call(self, token: str, name: str, args: dict, request_id: int = 100,
                  timeout: int = 10) -> dict:
        status, payload = _mcp(self.base, "tools/call", {"name": name, "arguments": args},
                               token=token, request_id=request_id, timeout=timeout)
        self.assertEqual(status, HTTPStatus.OK, f"tools/call {name} HTTP {status}: {payload}")
        result = (payload or {}).get("result") or {}
        if result.get("isError"):
            return {"isError": True, "error": _tool_error_text(result)}
        return {"isError": False, "result": result.get("structuredContent")}

    def _assert_ok(self, token: str, name: str, args: dict, request_id: int = 100) -> dict:
        resp = self._mcp_call(token, name, args, request_id)
        self.assertFalse(resp["isError"], f"{name} failed: {resp}")
        return resp["result"]

    def _pair(self, prefix: str, **create_kwargs) -> tuple[dict, dict, dict]:
        owner = self._signup(f"{prefix}-owner@example.com")
        member = self._signup(f"{prefix}-member@example.com", tenant_id=owner["tenant_id"])
        created = self._assert_ok(owner["session_token"], "room_create",
                                  {"cap": 4, **create_kwargs}, request_id=1)
        self._assert_ok(member["session_token"], "room_join",
                        {"room_id": created["room_id"],
                         "link_token": created["link_token"], "consent": True},
                        request_id=2)
        return owner, member, created


class RoomLeaveSurfaceTests(ProductPerfectTestBase):
    def test_member_can_leave_via_hosted_mcp(self) -> None:
        owner, member, created = self._pair("leave")
        left = self._assert_ok(member["session_token"], "room_leave",
                               {"room_id": created["room_id"]}, request_id=10)
        self.assertEqual(left["status"], "left")
        info = self._assert_ok(owner["session_token"], "room_info",
                               {"room_id": created["room_id"]}, request_id=11)
        self.assertEqual(info["member_count"], 1,
                         "leaving must free the seat (only the owner remains)")
        ids = {m["agent_id"] for m in info["members"]}
        self.assertNotIn(member["account_id"], ids)
        # A left member is no longer a member: polling must be refused.
        resp = self._mcp_call(member["session_token"], "room_poll",
                              {"room_id": created["room_id"]}, request_id=12)
        self.assertTrue(resp["isError"], "a left member must lose room access")

    def test_non_member_cannot_leave(self) -> None:
        owner, _, created = self._pair("leavenon")
        outsider = self._signup("leavenon-out@example.com", tenant_id=owner["tenant_id"])
        resp = self._mcp_call(outsider["session_token"], "room_leave",
                              {"room_id": created["room_id"]}, request_id=10)
        self.assertTrue(resp["isError"], "non-member leave must be refused")


class RoomTTLEnforcementTests(ProductPerfectTestBase):
    def test_expired_room_closes_lazily_and_refuses_sends(self) -> None:
        owner, member, created = self._pair("ttl", ttl_seconds=1)
        time.sleep(1.5)
        resp = self._mcp_call(member["session_token"], "room_send",
                              {"room_id": created["room_id"], "target_spec": "*",
                               "payload": {"text": "too-late"}}, request_id=10)
        self.assertTrue(resp["isError"], "send to an expired room must be refused")
        self.assertIn(resp["error"]["code"], ("room_closed", "room_expired"))
        info = self._assert_ok(owner["session_token"], "room_info",
                               {"room_id": created["room_id"]}, request_id=11)
        self.assertEqual(info["state"], "closed",
                         "the expired room must be lazily closed, not left active")
        # History remains readable (reads keep working on closed rooms).
        polled = self._assert_ok(owner["session_token"], "room_poll",
                                 {"room_id": created["room_id"], "after_seq": 0},
                                 request_id=12)
        self.assertGreaterEqual(polled["cursor_head"], 1,
                                "history must remain readable after closure")


class MessageKindSurfaceTests(ProductPerfectTestBase):
    def test_send_message_kind_roundtrips_and_filters(self) -> None:
        owner, member, created = self._pair("mk")
        sent = self._assert_ok(owner["session_token"], "room_send",
                               {"room_id": created["room_id"], "target_spec": "*",
                                "payload": {"text": "tagged"}, "message_kind": "task-update"},
                               request_id=10)
        seq = sent["seq"]
        # The event carries the kind.
        polled = self._assert_ok(member["session_token"], "room_poll",
                                 {"room_id": created["room_id"], "after_seq": 0},
                                 request_id=11)
        kinds = {e.get("message_kind") for e in polled["events"] if e.get("seq") == seq}
        self.assertIn("task-update", kinds,
                      "the send's message_kind must be stored on the event")
        # The poll filter matches on it.
        filtered = self._assert_ok(member["session_token"], "room_poll",
                                   {"room_id": created["room_id"], "after_seq": 0,
                                    "message_kinds": ["task-update"]}, request_id=12)
        filtered_seqs = [e["seq"] for e in filtered["events"]]
        self.assertIn(seq, filtered_seqs, "message_kinds filter must match the tagged event")
        # An unrelated filter excludes it.
        other = self._assert_ok(member["session_token"], "room_poll",
                                {"room_id": created["room_id"], "after_seq": 0,
                                 "message_kinds": ["other-kind"]}, request_id=13)
        self.assertNotIn(seq, [e["seq"] for e in other["events"]],
                         "an unrelated filter must exclude the tagged event")


class RateLimitProofTests(ProductPerfectTestBase):
    def test_room_message_budget_crossing_refuses_with_retry_after(self) -> None:
        owner, member, created = self._pair("rl")
        refused = None
        for i in range(75):
            resp = self._mcp_call(owner["session_token"], "room_send",
                                  {"room_id": created["room_id"], "target_spec": "*",
                                   "payload": {"text": f"burst-{i}"}}, request_id=1000 + i)
            if resp["isError"]:
                refused = resp
                break
        self.assertIsNotNone(refused, "crossing 60 sends/min must refuse")
        self.assertEqual(refused["error"]["code"], "rate_limited",
                         "the refusal must name rate_limited")
        self.assertIn("retry_after", refused["error"],
                      "the refusal must carry a retry_after the caller can act on")


class RoomRemoveMemberTests(ProductPerfectTestBase):
    def test_owner_removes_member_frees_seat_and_revokes_access(self) -> None:
        owner, member, created = self._pair("rm")
        removed = self._assert_ok(owner["session_token"], "room_remove_member",
                                  {"room_id": created["room_id"],
                                   "member_id": member["account_id"]}, request_id=10)
        self.assertEqual(removed["status"], "left")
        info = self._assert_ok(owner["session_token"], "room_info",
                               {"room_id": created["room_id"]}, request_id=11)
        self.assertEqual(info["member_count"], 1, "removal must free the seat")
        resp = self._mcp_call(member["session_token"], "room_poll",
                              {"room_id": created["room_id"]}, request_id=12)
        self.assertTrue(resp["isError"], "the removed member must be refused on its next request")

    def test_removed_member_can_rejoin_not_a_ban(self) -> None:
        owner, member, created = self._pair("rmrejoin")
        self._assert_ok(owner["session_token"], "room_remove_member",
                        {"room_id": created["room_id"],
                         "member_id": member["account_id"]}, request_id=10)
        rejoined = self._assert_ok(member["session_token"], "room_join",
                                   {"room_id": created["room_id"],
                                    "link_token": created["link_token"], "consent": True},
                                   request_id=11)
        self.assertEqual(rejoined["status"], "active",
                         "removal is not a ban: a valid link admits the member again")

    def test_non_owner_cannot_remove(self) -> None:
        owner, member, created = self._pair("rmno")
        third = self._signup("rmno-third@example.com", tenant_id=owner["tenant_id"])
        self._assert_ok(third["session_token"], "room_join",
                        {"room_id": created["room_id"],
                         "link_token": created["link_token"], "consent": True}, request_id=10)
        resp = self._mcp_call(third["session_token"], "room_remove_member",
                              {"room_id": created["room_id"],
                               "member_id": member["account_id"]}, request_id=11)
        self.assertTrue(resp["isError"], "a non-owner must be refused")
        self.assertEqual(resp["error"]["code"], "owner_required")

    def test_owner_cannot_remove_self(self) -> None:
        owner, _, created = self._pair("rmself")
        resp = self._mcp_call(owner["session_token"], "room_remove_member",
                              {"room_id": created["room_id"],
                               "member_id": owner["account_id"]}, request_id=10)
        self.assertTrue(resp["isError"], "the owner cannot remove themselves")
        self.assertEqual(resp["error"]["code"], "owner_required")

    def test_unknown_target_not_found(self) -> None:
        owner, _, created = self._pair("rmghost")
        resp = self._mcp_call(owner["session_token"], "room_remove_member",
                              {"room_id": created["room_id"],
                               "member_id": "acct_does_not_exist"}, request_id=10)
        self.assertTrue(resp["isError"], "an unknown target must be refused")
        self.assertEqual(resp["error"]["code"], "member_not_found")

    def test_removal_event_recorded_with_reason(self) -> None:
        owner, member, created = self._pair("rmevent")
        self._assert_ok(owner["session_token"], "room_remove_member",
                        {"room_id": created["room_id"],
                         "member_id": member["account_id"]}, request_id=10)
        log = self._assert_ok(owner["session_token"], "room_event_log",
                              {"room_id": created["room_id"]}, request_id=11)
        left_events = [e for e in log["events"]
                       if e.get("payload") and e["payload"].get("reason") == "removed_by_owner"]
        self.assertEqual(len(left_events), 1,
                         "the removal must emit exactly one room.left with reason removed_by_owner")


class RoomTTLQuotaTests(ProductPerfectTestBase):
    def test_ttl_close_releases_the_rooms_quota_slot(self) -> None:
        owner, _, created = self._pair("ttlquota", ttl_seconds=1)

        def rooms_counter() -> int:
            with self.service.backend.transaction() as tx:
                row = tx.execute(
                    "SELECT value FROM cloud_counters WHERE tenant_id = ? AND counter = 'rooms'",
                    (owner["tenant_id"],),
                ).fetchone()
            return int(row["value"]) if row else 0

        self.assertEqual(rooms_counter(), 1, "one active room is counted")
        time.sleep(1.5)
        resp = self._mcp_call(owner["session_token"], "room_send",
                              {"room_id": created["room_id"], "target_spec": "*",
                               "payload": {"text": "after-expiry"}}, request_id=10)
        self.assertTrue(resp["isError"], "send after expiry must be refused")
        self.assertEqual(rooms_counter(), 0,
                         "the lazy TTL close must release the tenant's active-room quota slot")


class CoordinatorRemoveMemberTests(unittest.TestCase):
    """Coordinator-plane parity for the owner remove_member surface."""

    def setUp(self) -> None:
        from weft_mcp.core import WeftStore
        from weft_mcp.server import WeftDispatcher

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        self.team = "team-coord-remove"
        self.tokens = {}
        for agent_id in ("OWNER", "A2"):
            reg = self.dispatcher.call_tool(
                "register_agent",
                {"team_id": self.team, "agent_id": agent_id, "role": "member"},
            )
            self.tokens[agent_id] = reg["actor_token"]
        created = self.dispatcher.call_tool(
            "room_create", {
                "team_id": self.team,
                "owner_agent_id": "OWNER",
                "cap": 5,
                "actor_token": self.tokens["OWNER"],
            },
        )
        self.room_id = created["room_id"]
        self.link_token = created["link_token"]
        for agent_id in ("OWNER", "A2"):
            self.dispatcher.call_tool("room_join", {
                "team_id": self.team, "room_id": self.room_id,
                "link_token": self.link_token, "agent_id": agent_id,
                "consent": True, "actor_token": self.tokens[agent_id],
            })

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_owner_removes_member_and_it_can_rejoin(self) -> None:
        removed = self.dispatcher.call_tool("room_remove_member", {
            "team_id": self.team, "room_id": self.room_id,
            "owner_agent_id": "OWNER", "target_agent_id": "A2",
            "actor_token": self.tokens["OWNER"],
        })
        self.assertEqual(removed["status"], "left")
        from weft_mcp.core import WeftError
        with self.assertRaises(WeftError):
            self.dispatcher.call_tool("room_poll", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "A2", "actor_token": self.tokens["A2"], "after_seq": 0,
            })
        rejoined = self.dispatcher.call_tool("room_join", {
            "team_id": self.team, "room_id": self.room_id,
            "link_token": self.link_token, "agent_id": "A2",
            "consent": True, "actor_token": self.tokens["A2"],
        })
        self.assertEqual(rejoined["status"], "active")

    def test_non_owner_refused_and_self_removal_refused(self) -> None:
        from weft_mcp.core import WeftError
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool("room_remove_member", {
                "team_id": self.team, "room_id": self.room_id,
                "owner_agent_id": "A2", "target_agent_id": "OWNER",
                "actor_token": self.tokens["A2"],
            })
        self.assertEqual(ctx.exception.code, "owner_required")
        with self.assertRaises(WeftError) as ctx2:
            self.dispatcher.call_tool("room_remove_member", {
                "team_id": self.team, "room_id": self.room_id,
                "owner_agent_id": "OWNER", "target_agent_id": "OWNER",
                "actor_token": self.tokens["OWNER"],
            })
        self.assertEqual(ctx2.exception.code, "owner_required")

    def test_ghost_target_not_found(self) -> None:
        from weft_mcp.core import WeftError
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool("room_remove_member", {
                "team_id": self.team, "room_id": self.room_id,
                "owner_agent_id": "OWNER", "target_agent_id": "ghost",
                "actor_token": self.tokens["OWNER"],
            })
        self.assertEqual(ctx.exception.code, "member_not_found")


class CoordinatorTTLEnforcementTests(unittest.TestCase):
    """Coordinator parity: the room TTL closes the ROOM, not just the link."""

    def test_expired_room_closes_lazily_on_first_send(self) -> None:
        import sqlite3 as _sqlite3

        from weft_mcp.core import WeftError, WeftStore
        from weft_mcp.server import WeftDispatcher

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        self.team = "team-coord-ttl"
        reg = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": self.team, "agent_id": "OWNER", "role": "member"},
        )
        created = self.dispatcher.call_tool(
            "room_create", {"team_id": self.team, "owner_agent_id": "OWNER",
                            "cap": 4, "ttl_seconds": 1,
                            "actor_token": reg["actor_token"]},
        )
        self.room_id = created["room_id"]
        try:
            time.sleep(1.5)
            with self.assertRaises(WeftError) as ctx:
                self.dispatcher.call_tool("room_send", {
                    "team_id": self.team, "room_id": self.room_id,
                    "sender_agent_id": "OWNER", "target_spec": "*",
                    "payload": {"text": "too-late"}, "actor_token": reg["actor_token"],
                })
            self.assertEqual(ctx.exception.code, "room_closed")
            info = self.dispatcher.call_tool("room_info", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "OWNER", "actor_token": reg["actor_token"],
            })
            self.assertEqual(info["state"], "closed",
                             "the expired coordinator room must be lazily closed")
            polled = self.dispatcher.call_tool("room_poll", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "OWNER", "actor_token": reg["actor_token"], "after_seq": 0,
            })
            reasons = [e["payload"].get("reason") for e in polled["events"]
                       if e["kind"] == "room.closed" and e.get("payload")]
            self.assertIn("ttl_expired", reasons,
                          "the close event must carry reason ttl_expired")
        finally:
            self.store.close()
            self.temp.cleanup()


if __name__ == "__main__":
    unittest.main()
