"""Cursor guards — silence must never mean success, for READS as well as writes.

Post-deploy finding (F14, measured by both parties on production): a poll with
an ``after_seq`` above the room head silently returned ``events: [], next_seq:
<the bogus value>`` — the server ECHOED the caller's impossible cursor as if it
were real, and a client that jumped its window past unread events was never
told it had skipped anything. An agent cannot fix its own call from that
response; it cannot even tell there is anything to fix.

The fix under test, both planes:

  - ``after_seq > cursor_head`` is REFUSED with ``invalid_cursor`` (400) —
    never echoed;
  - negative ``after_seq`` is refused the same way (no silent clamping);
  - poll/wait responses include ``behind_by``: the number of events between
    the caller's ``last_ack_seq`` and ``after_seq`` that the caller skipped
    without acking. Zero means the window is cursor-consistent.

These tests drive the real hosted HTTP surface (cloud) and the real MCP
dispatcher (coordinator), with no mocks for the SQLite layer.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import threading
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


class CloudCursorGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from http.server import ThreadingHTTPServer

        cls.tmpdir = tempfile.mkdtemp(prefix="weft-cursorguard-test-")
        cls.db_path = str(Path(cls.tmpdir) / "test.db")
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        cls.port = cls._httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        # This class exercises cursor behavior, not signup throttling. Keep
        # the shared fixture below the production code path while allowing
        # the full cursor matrix to create its deliberately isolated users.
        cls.service = WeftCloudService(
            SqliteWalBackend(cls.db_path),
            auth_rate_limits={"signup": {"ip": 100, "email": 100}},
        )
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

    def _pair(self, prefix: str) -> tuple[dict, dict, dict]:
        owner = self._signup(f"{prefix}-owner@example.com")
        member = self._signup(f"{prefix}-member@example.com", tenant_id=owner["tenant_id"])
        created = self._assert_ok(owner["session_token"], "room_create", {"cap": 4}, request_id=1)
        self._assert_ok(member["session_token"], "room_join",
                        {"room_id": created["room_id"],
                         "link_token": created["link_token"], "consent": True}, request_id=2)
        return owner, member, created

    def test_poll_beyond_head_refused_not_echoed(self) -> None:
        owner, member, created = self._pair("bogus")
        resp = self._mcp_call(member["session_token"], "room_poll",
                              {"room_id": created["room_id"], "after_seq": 99999}, request_id=10)
        self.assertTrue(resp["isError"], "poll beyond head must be refused")
        self.assertEqual(resp["error"]["code"], "invalid_cursor")

    def test_poll_negative_after_seq_refused(self) -> None:
        owner, member, created = self._pair("neg")
        resp = self._mcp_call(member["session_token"], "room_poll",
                              {"room_id": created["room_id"], "after_seq": -5}, request_id=10)
        self.assertTrue(resp["isError"], "negative after_seq must be refused")
        self.assertEqual(resp["error"]["code"], "invalid_cursor")

    def test_ack_negative_seq_refused_without_poisoning_default_cursor(self) -> None:
        owner, member, created = self._pair("negack")
        resp = self._mcp_call(member["session_token"], "room_ack",
                              {"room_id": created["room_id"], "seq": -1}, request_id=10)
        self.assertTrue(resp["isError"], "negative ack seq must be refused")
        self.assertEqual(resp["error"]["code"], "invalid_cursor")
        polled = self._assert_ok(member["session_token"], "room_poll",
                                 {"room_id": created["room_id"]}, request_id=11)
        self.assertGreaterEqual(polled["last_ack_seq"], 0,
                                "a rejected ack must not persist a negative cursor")

    def test_behind_by_reports_skipped_window(self) -> None:
        owner, member, created = self._pair("gap")
        # Send three events; the member acks only the first.
        for text in ("one", "two", "three"):
            self._assert_ok(owner["session_token"], "room_send",
                            {"room_id": created["room_id"], "target_spec": "*",
                             "payload": {"text": text}}, request_id=10)
        head = self._assert_ok(member["session_token"], "room_poll",
                               {"room_id": created["room_id"], "after_seq": 0}, request_id=11)["cursor_head"]
        self._assert_ok(member["session_token"], "room_ack",
                        {"room_id": created["room_id"], "seq": 1}, request_id=12)
        polled = self._assert_ok(member["session_token"], "room_poll",
                                 {"room_id": created["room_id"], "after_seq": head}, request_id=13)
        self.assertEqual(polled["behind_by"], head - 1,
                         "skipping from ack 1 to after_seq head must report the gap")

    def test_behind_by_zero_for_cursor_default(self) -> None:
        owner, member, created = self._pair("zero")
        polled = self._assert_ok(member["session_token"], "room_poll",
                                 {"room_id": created["room_id"]}, request_id=10)
        self.assertEqual(polled["behind_by"], 0,
                         "cursor-default polling must report behind_by 0")

    def test_wait_beyond_head_refused(self) -> None:
        owner, member, created = self._pair("waitbogus")
        resp = self._mcp_call(member["session_token"], "room_wait",
                              {"room_id": created["room_id"], "after_seq": 99999,
                               "timeout_seconds": 1}, request_id=10)
        self.assertTrue(resp["isError"], "wait beyond head must be refused")
        self.assertEqual(resp["error"]["code"], "invalid_cursor")

    def test_end_of_stream_marker_head_plus_one_is_allowed(self) -> None:
        """The paging contract: next_seq returned by a poll is head+1 at the
        tail, and passing it back must be accepted (empty page), not refused —
        the reconnect suite depends on exactly this."""
        owner, member, created = self._pair("eof")
        self._assert_ok(owner["session_token"], "room_send",
                        {"room_id": created["room_id"], "target_spec": "*",
                         "payload": {"text": "tail"}}, request_id=10)
        first = self._assert_ok(member["session_token"], "room_poll",
                                {"room_id": created["room_id"], "after_seq": 0}, request_id=11)
        head = first["cursor_head"]
        self.assertEqual(first["next_seq"], head + 1)
        second = self._assert_ok(member["session_token"], "room_poll",
                                 {"room_id": created["room_id"],
                                  "after_seq": first["next_seq"]}, request_id=12)
        self.assertEqual(second["events"], [])
        self.assertEqual(second["next_seq"], head + 1,
                         "an empty page at the tail must echo the stable end-of-stream marker")

    def test_resume_marker_catches_first_event_after_idle_poll(self) -> None:
        owner, member, created = self._pair("tail-catch-up")
        first = self._assert_ok(member["session_token"], "room_poll",
                                {"room_id": created["room_id"], "after_seq": 0},
                                request_id=10)
        marker = first["next_seq"]
        empty = self._assert_ok(member["session_token"], "room_poll",
                                {"room_id": created["room_id"], "after_seq": marker},
                                request_id=11)
        self.assertEqual(empty["events"], [])
        sent = self._assert_ok(owner["session_token"], "room_send",
                               {"room_id": created["room_id"], "target_spec": "*",
                                "payload": {"text": "tail-catch-up"}},
                               request_id=12)
        self.assertEqual(sent["seq"], marker)
        resumed = self._assert_ok(member["session_token"], "room_poll",
                                   {"room_id": created["room_id"], "after_seq": marker},
                                   request_id=13)
        self.assertEqual([event["seq"] for event in resumed["events"]], [marker])
        replayed = self._assert_ok(member["session_token"], "room_poll",
                                   {"room_id": created["room_id"]}, request_id=14)
        self.assertIn(marker, [event["seq"] for event in replayed["events"]])
        self._assert_ok(member["session_token"], "room_ack",
                        {"room_id": created["room_id"], "seq": marker}, request_id=15)
        after_ack = self._assert_ok(member["session_token"], "room_poll",
                                    {"room_id": created["room_id"], "after_seq": marker},
                                    request_id=16)
        self.assertEqual(after_ack["events"], [])

    def test_resume_marker_does_not_skip_truncated_page(self) -> None:
        owner, member, created = self._pair("truncated-page")
        first = self._assert_ok(member["session_token"], "room_poll",
                                {"room_id": created["room_id"], "after_seq": 0,
                                 "limit": 1}, request_id=10)
        marker = first["next_seq"]
        self.assertEqual(len(first["events"]), 1)
        second = self._assert_ok(member["session_token"], "room_poll",
                                 {"room_id": created["room_id"], "after_seq": marker,
                                  "limit": 1}, request_id=11)
        self.assertEqual([event["seq"] for event in second["events"]], [marker])

    def test_filtered_empty_page_reports_full_stream_cursor(self) -> None:
        owner, member, created = self._pair("filtered-empty")
        status, sent = _post(self.base, "/v1/rooms/send", {
            "room_id": created["room_id"],
            "target_spec": member["account_id"],
            "payload": {"text": "status-only"},
            "message_kind": "status",
        }, token=owner["session_token"])
        self.assertEqual(status, HTTPStatus.OK, sent)

        status, filtered = _post(self.base, "/v1/rooms/poll", {
            "room_id": created["room_id"],
            "after_seq": 0,
            "message_kinds": ["result"],
        }, token=member["session_token"])
        self.assertEqual(status, HTTPStatus.OK, filtered)
        self.assertEqual(filtered["events"], [])
        self.assertEqual(
            filtered["next_seq"], filtered["cursor_head"],
            "a filtered page that scanned the stream must report the full-stream cursor",
        )

    def test_hosted_mcp_message_kind_filter_round_trip(self) -> None:
        owner, member, created = self._pair("mcp-filter")
        sent = self._assert_ok(owner["session_token"], "room_send", {
            "room_id": created["room_id"],
            "target_spec": member["account_id"],
            "payload": {"text": "status-only"},
            "message_kind": "status",
        }, request_id=10)

        filtered = self._assert_ok(member["session_token"], "room_poll", {
            "room_id": created["room_id"],
            "after_seq": 0,
            "message_kinds": ["result"],
        }, request_id=11)
        self.assertEqual(filtered["events"], [])
        self.assertEqual(filtered["next_seq"], filtered["cursor_head"])

        matching = self._assert_ok(member["session_token"], "room_poll", {
            "room_id": created["room_id"],
            "after_seq": 0,
            "message_kinds": ["status"],
        }, request_id=12)
        self.assertIn(sent["seq"], [event["seq"] for event in matching["events"]])

    def test_json_rpc_rejects_non_object_params(self) -> None:
        account = self._signup("params-shape@example.com")
        for malformed in (False, []):
            with self.subTest(params=malformed):
                status, response = _mcp(
                    self.base, "initialize", malformed,
                    token=account["session_token"], request_id=10,
                )
                self.assertEqual(status, HTTPStatus.OK, response)
                self.assertEqual(response["error"]["code"], -32600)


class CoordinatorCursorGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        from weft_mcp.core import WeftStore
        from weft_mcp.server import WeftDispatcher

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        self.team = "team-cursorguard"
        self.tokens = {}
        for agent_id in ("OWNER", "A2"):
            reg = self.dispatcher.call_tool(
                "register_agent",
                {"team_id": self.team, "agent_id": agent_id, "role": "member"},
            )
            self.tokens[agent_id] = reg["actor_token"]
        created = self.dispatcher.call_tool(
            "room_create", {"team_id": self.team, "owner_agent_id": "OWNER", "cap": 5,
                             "actor_token": self.tokens["OWNER"]},
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

    def test_poll_beyond_head_refused(self) -> None:
        from weft_mcp.core import WeftError
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool("room_poll", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "A2", "actor_token": self.tokens["A2"], "after_seq": 99999,
            })
        self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_poll_negative_after_seq_refused(self) -> None:
        from weft_mcp.core import WeftError
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool("room_poll", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "A2", "actor_token": self.tokens["A2"], "after_seq": -5,
            })
        self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_ack_negative_seq_refused_without_poisoning_default_cursor(self) -> None:
        from weft_mcp.core import WeftError
        with self.assertRaises(WeftError) as ctx:
            self.dispatcher.call_tool("room_ack", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "A2", "actor_token": self.tokens["A2"], "seq": -1,
            })
        self.assertEqual(ctx.exception.code, "invalid_cursor")
        polled = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"],
        })
        self.assertGreaterEqual(polled["last_ack_seq"], 0,
                                "a rejected ack must not persist a negative cursor")

    def test_behind_by_reports_skipped_window(self) -> None:
        self.dispatcher.call_tool("room_send", {
            "team_id": self.team, "room_id": self.room_id,
            "sender_agent_id": "OWNER", "target_spec": "*",
            "payload": {"text": "one"}, "actor_token": self.tokens["OWNER"],
        })
        head = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"], "after_seq": 0,
        })["cursor_head"]
        self.dispatcher.call_tool("room_ack", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "seq": 1, "actor_token": self.tokens["A2"],
        })
        polled = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"], "after_seq": head,
        })
        self.assertEqual(polled["behind_by"], head - 1)

    def test_poll_accepts_next_seq_after_empty_page(self) -> None:
        first = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"], "after_seq": 0,
        })
        second = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"],
            "after_seq": first["next_seq"],
        })
        self.assertEqual(second["events"], [])
        self.assertEqual(second["next_seq"], first["next_seq"])

    def test_resume_marker_catches_first_event_after_idle_poll(self) -> None:
        first = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"], "after_seq": 0,
        })
        marker = first["next_seq"]
        empty = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"], "after_seq": marker,
        })
        self.assertEqual(empty["events"], [])
        sent = self.dispatcher.call_tool("room_send", {
            "team_id": self.team, "room_id": self.room_id,
            "sender_agent_id": "OWNER", "target_spec": "*",
            "payload": {"text": "tail-catch-up"},
            "actor_token": self.tokens["OWNER"],
        })
        self.assertEqual(sent["seq"], marker)
        resumed = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"], "after_seq": marker,
        })
        self.assertEqual([event["seq"] for event in resumed["events"]], [marker])
        replayed = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"],
        })
        self.assertIn(marker, [event["seq"] for event in replayed["events"]])
        self.dispatcher.call_tool("room_ack", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "seq": marker, "actor_token": self.tokens["A2"],
        })
        after_ack = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"], "after_seq": marker,
        })
        self.assertEqual(after_ack["events"], [])

    def test_resume_marker_does_not_skip_truncated_page(self) -> None:
        first = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"],
            "after_seq": 0, "limit": 1,
        })
        marker = first["next_seq"]
        self.assertEqual(len(first["events"]), 1)
        second = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"],
            "after_seq": marker, "limit": 1,
        })
        self.assertEqual([event["seq"] for event in second["events"]], [marker])

    def test_filtered_empty_page_reports_full_stream_cursor(self) -> None:
        self.dispatcher.call_tool("room_send", {
            "team_id": self.team, "room_id": self.room_id,
            "sender_agent_id": "OWNER", "target_spec": "A2",
            "payload": {"text": "status-only"}, "message_kind": "status",
            "actor_token": self.tokens["OWNER"],
        })
        filtered = self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"],
            "after_seq": 0, "message_kinds": ["result"],
        })
        self.assertEqual(filtered["events"], [])
        self.assertEqual(
            filtered["next_seq"], filtered["cursor_head"],
            "a filtered page that scanned the stream must report the full-stream cursor",
        )


if __name__ == "__main__":
    unittest.main()
