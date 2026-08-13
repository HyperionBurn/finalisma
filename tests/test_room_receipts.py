"""Receipt lifecycle — the send response's 'queued' is no longer a hardcoded literal.

Regression suite for the receipt state machine (cloud plane):

    - every send persists ONE cloud_room_receipts row per routed target
      (status 'queued'), committed in the same transaction as the event;
    - a recipient's ack past an event's seq transitions that recipient's
      receipt queued -> read — and ONLY theirs;
    - the ack response reports how many of the caller's receipts are read,
      so a recipient can prove consumption without a new query surface;
    - receipts survive a service restart (durable rows, not derived state).

Tests drive the REAL hosted HTTP surface, with receipt rows read straight
from the storage backend (no mocks for the SQLite layer).
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


class RoomReceiptsTestBase(unittest.TestCase):
    """Real cloud HTTP service on a background thread (shared per class)."""

    @classmethod
    def setUpClass(cls) -> None:
        from http.server import ThreadingHTTPServer

        cls.tmpdir = tempfile.mkdtemp(prefix="weft-receipts-test-")
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

    # -- helpers ----------------------------------------------------------

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

    def _three_members(self, prefix: str) -> tuple[dict, dict, dict, dict]:
        """Room owned by ``prefix-owner`` with ``prefix-a`` and ``prefix-b`` joined."""
        owner = self._signup(f"{prefix}-owner@example.com")
        a = self._signup(f"{prefix}-a@example.com", tenant_id=owner["tenant_id"])
        b = self._signup(f"{prefix}-b@example.com", tenant_id=owner["tenant_id"])
        created = self._assert_ok(owner["session_token"], "room_create", {"cap": 5}, request_id=1)
        for member in (a, b):
            self._assert_ok(member["session_token"], "room_join",
                            {"room_id": created["room_id"],
                             "link_token": created["link_token"], "consent": True},
                            request_id=2)
        return owner, a, b, created

    def _receipt_rows(self, tenant_id: str, room_id: str, recipient: str | None = None,
                       seq: int | None = None) -> list:
        with self.service.backend.transaction() as tx:
            if recipient is not None and seq is not None:
                rows = tx.execute(
                    "SELECT * FROM cloud_room_receipts WHERE tenant_id = ? AND room_id = ? "
                    "AND recipient_agent_id = ? AND seq = ?",
                    (tenant_id, room_id, recipient, seq),
                ).fetchall()
            elif recipient is not None:
                rows = tx.execute(
                    "SELECT * FROM cloud_room_receipts WHERE tenant_id = ? AND room_id = ? "
                    "AND recipient_agent_id = ?",
                    (tenant_id, room_id, recipient),
                ).fetchall()
            else:
                rows = tx.execute(
                    "SELECT * FROM cloud_room_receipts WHERE tenant_id = ? AND room_id = ?",
                    (tenant_id, room_id),
                ).fetchall()
            return list(rows)


class RoomReceiptLifecycleTests(RoomReceiptsTestBase):
    def test_send_persists_one_receipt_row_per_target(self) -> None:
        owner, a, b, created = self._three_members("rows")
        sent = self._assert_ok(owner["session_token"], "room_send",
                               {"room_id": created["room_id"], "target_spec": "*",
                                "payload": {"text": "broadcast-rows"}}, request_id=10)
        seq = sent["seq"]
        rows = self._receipt_rows(owner["tenant_id"], created["room_id"], seq=seq)
        recipients = sorted(r["recipient_agent_id"] for r in rows)
        # exclude_sender defaults to True: the owner is not a target of its own broadcast.
        self.assertEqual(recipients, sorted([a["account_id"], b["account_id"]]),
                         "one receipt row per routed target, sender excluded")
        for r in rows:
            self.assertEqual(r["status"], "queued")
            self.assertEqual(r["sender_agent_id"], owner["account_id"])

    def test_ack_marks_own_receipts_read_only(self) -> None:
        owner, a, b, created = self._three_members("scoped")
        sent = self._assert_ok(owner["session_token"], "room_send",
                               {"room_id": created["room_id"], "target_spec": "*",
                                "payload": {"text": "scoped-broadcast"}}, request_id=10)
        seq = sent["seq"]
        acked = self._assert_ok(a["session_token"], "room_ack",
                                {"room_id": created["room_id"], "seq": seq}, request_id=11)
        self.assertEqual(acked["receipts_read"], 1,
                         "ack response must report the caller's read receipts")
        a_row = self._receipt_rows(owner["tenant_id"], created["room_id"],
                                   recipient=a["account_id"], seq=seq)
        b_row = self._receipt_rows(owner["tenant_id"], created["room_id"],
                                   recipient=b["account_id"], seq=seq)
        self.assertEqual(a_row[0]["status"], "read", "acker's own receipt must transition to read")
        self.assertEqual(b_row[0]["status"], "queued",
                         "another recipient's receipt must be untouched by a's ack")

    def test_ack_below_seq_keeps_later_receipts_queued(self) -> None:
        owner, a, _, created = self._three_members("partial")
        sent1 = self._assert_ok(owner["session_token"], "room_send",
                                {"room_id": created["room_id"], "target_spec": a["account_id"],
                                 "payload": {"text": "first"}}, request_id=10)
        sent2 = self._assert_ok(owner["session_token"], "room_send",
                                {"room_id": created["room_id"], "target_spec": a["account_id"],
                                 "payload": {"text": "second"}}, request_id=11)
        self._assert_ok(a["session_token"], "room_ack",
                        {"room_id": created["room_id"], "seq": sent1["seq"]}, request_id=12)
        first = self._receipt_rows(owner["tenant_id"], created["room_id"],
                                   recipient=a["account_id"], seq=sent1["seq"])
        second = self._receipt_rows(owner["tenant_id"], created["room_id"],
                                    recipient=a["account_id"], seq=sent2["seq"])
        self.assertEqual(first[0]["status"], "read")
        self.assertEqual(second[0]["status"], "queued",
                         "acking an earlier seq must not read later receipts")

    def test_receipts_survive_service_restart(self) -> None:
        owner, a, _, created = self._three_members("durable")
        sent = self._assert_ok(owner["session_token"], "room_send",
                               {"room_id": created["room_id"], "target_spec": a["account_id"],
                                "payload": {"text": "durable"}}, request_id=10)
        self._assert_ok(a["session_token"], "room_ack",
                        {"room_id": created["room_id"], "seq": sent["seq"]}, request_id=11)
        self.service.backend.close()
        self.service.backend = SqliteWalBackend(self.db_path)
        rows = self._receipt_rows(owner["tenant_id"], created["room_id"],
                                  recipient=a["account_id"], seq=sent["seq"])
        self.assertEqual(rows[0]["status"], "read",
                         "receipt lifecycle must be durable across a service restart")

    def test_unknown_target_creates_no_receipt_row(self) -> None:
        owner, _, _, created = self._three_members("ghost")
        resp = self._mcp_call(owner["session_token"], "room_send",
                              {"room_id": created["room_id"], "target_spec": "key_ghost_0000",
                               "payload": {"text": "ghost"}}, request_id=10)
        self.assertTrue(resp["isError"], "unknown target must be refused")
        self.assertEqual(resp["error"]["code"], "recipient_not_found")
        rows = self._receipt_rows(owner["tenant_id"], created["room_id"],
                                  recipient="key_ghost_0000")
        self.assertEqual(rows, [], "a refused send must leave no receipt rows")


# ---------------------------------------------------------------------------
# Coordinator plane — same receipt lifecycle, real MCP dispatcher.
# ---------------------------------------------------------------------------

class CoordinatorRoomReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        import sqlite3 as _sqlite3

        from weft_mcp.core import WeftStore
        from weft_mcp.server import WeftDispatcher

        self._sqlite3 = _sqlite3
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WeftStore(self.root / "state.db", self.root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        self.team = "team-coord-receipts"
        self.tokens = {}
        for agent_id in ("OWNER", "A2"):
            reg = self.dispatcher.call_tool(
                "register_agent",
                {"team_id": self.team, "agent_id": agent_id, "role": "member"},
            )
            self.tokens[agent_id] = reg["actor_token"]
        created = self.dispatcher.call_tool(
            "room_create", {"team_id": self.team, "owner_agent_id": "OWNER", "cap": 5},
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

    def _receipt_rows(self, recipient: str | None = None) -> list:
        conn = self._sqlite3.connect(self.dispatcher.rooms.db_path)
        try:
            if recipient is None:
                return conn.execute(
                    "SELECT * FROM room_receipts WHERE room_id = ?",
                    (self.room_id,),
                ).fetchall()
            return conn.execute(
                "SELECT * FROM room_receipts WHERE room_id = ? AND recipient_agent_id = ?",
                (self.room_id, recipient),
            ).fetchall()
        finally:
            conn.close()

    def _send(self, sender: str, target_spec, payload: dict) -> dict:
        return self.dispatcher.call_tool("room_send", {
            "team_id": self.team, "room_id": self.room_id,
            "sender_agent_id": sender, "target_spec": target_spec,
            "payload": payload, "actor_token": self.tokens[sender],
        })

    def test_send_creates_receipt_row_and_ack_transitions_it(self) -> None:
        sent = self._send("OWNER", "A2", {"text": "coord-receipt"})
        rows = self._receipt_rows("A2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][5], "queued")  # status column
        self.assertIsNotNone(rows[0][4], "entry_id must be filled after the outbox enqueue")

        acked = self.dispatcher.call_tool("room_ack", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "seq": sent["seq"], "actor_token": self.tokens["A2"],
        })
        self.assertEqual(acked["receipts_read"], 1,
                         "coordinator ack must report the caller's read receipts")
        self.assertEqual(self._receipt_rows("A2")[0][5], "read")

    def test_ack_scoped_to_acker_only(self) -> None:
        sent = self._send("OWNER", "A2", {"text": "scoped"})
        owner_rows_before = self._receipt_rows("OWNER")
        acked = self.dispatcher.call_tool("room_ack", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "seq": sent["seq"], "actor_token": self.tokens["A2"],
        })
        self.assertEqual(acked["receipts_read"], 1)
        self.assertEqual(self._receipt_rows("A2")[0][5], "read")
        # The sender's own view is untouched: no receipt row exists for the
        # sender because the unicast excluded them.
        self.assertEqual(self._receipt_rows("OWNER"), owner_rows_before)

    def test_receipts_survive_store_reopen(self) -> None:
        sent = self._send("OWNER", "A2", {"text": "durable-coord"})
        self.dispatcher.call_tool("room_ack", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "seq": sent["seq"], "actor_token": self.tokens["A2"],
        })
        self.store.close()
        from weft_mcp.core import WeftStore
        from weft_mcp.server import WeftDispatcher
        self.store = WeftStore(self.root / "state.db", self.root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        rows = self._receipt_rows("A2")
        self.assertEqual(rows[0][5], "read",
                         "coordinator receipt lifecycle must survive a store reopen")


if __name__ == "__main__":
    unittest.main()
