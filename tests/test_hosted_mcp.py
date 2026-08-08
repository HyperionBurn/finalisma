"""Hosted MCP endpoint integration tests.

Drives the REAL ``WeftCloudService`` over REAL HTTP — the MCP client speaks
JSON-RPC to ``POST /mcp`` exactly as an MCP host (Claude Desktop, Cursor,
… ) would, with no weft_cloud internals on the wire path. Every room tool on
the hosted surface routes through ``CloudRoomService`` (the same service the
``/v1`` API drives), so a room created here IS the same room visible through
``/v1``.

The negative cases are the point of this surface:

  - an unauthenticated call is refused with a generic error that is byte-for-byte
    identical whether the token is absent, malformed, or unknown (no oracle),
  - tenant B reaching tenant A's room by room_id alone gets exactly the same
    error as a room that does not exist (no existence oracle),
  - identity arguments (agent_id / actor_token / team_id / …) are rejected:
    identity is always the authenticated session.

Authoritative spec: docs/HOSTED_MCP_DESIGN.md.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.identity.tokens import hash_token
from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend

HOSTED_TOOL_NAMES = [
    "room_create",
    "room_join",
    "room_send",
    "room_poll",
    "room_info",
    "room_ack",
    "room_heartbeat",
    "room_event_log",
]


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            pass
        return exc.code, payload


def _mcp(base: str, method: str, params: dict | None, token: str | None = None,
         request_id: int | None = 1, notification: bool = False) -> tuple[int, dict | None]:
    body: dict = {"jsonrpc": "2.0", "method": method}
    if not notification:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(base + "/mcp", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        payload = None
        try:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            pass
        return exc.code, payload


def _tool_error_text(result: dict) -> dict:
    """Pull the structured {'error': {...}} out of an isError tool result."""
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"code": "unparseable", "message": text}
    return parsed.get("error", {"code": "missing_error", "message": text})


class HostedMCPTestBase(unittest.TestCase):
    """Runs the real cloud HTTP service on a background thread.

    One server is started per test CLASS and shared by its tests. Tests stay
    isolated because every signup mints a fresh account/tenant (emails are
    per-test), so no two tests touch the same rows. Sharing the server keeps
    the suite's wall-clock cost flat as this surface grows.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.mkdtemp(prefix="weft-mcp-test-")
        cls.db_path = str(Path(cls.tmpdir) / "test.db")
        cls.service = WeftCloudService(SqliteWalBackend(cls.db_path))
        cls.port = 18900 + (hash(cls.__name__) % 500)
        cls._server_started = threading.Event()
        cls._httpd = None
        cls.server_thread = threading.Thread(target=cls._serve, daemon=True)
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.server_thread.start()
        cls._server_started.wait(timeout=5)

    @classmethod
    def _serve(cls) -> None:
        from http.server import ThreadingHTTPServer

        _CloudHTTPHandler.service = cls.service
        httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), _CloudHTTPHandler)
        cls._httpd = httpd
        cls._server_started.set()
        httpd.serve_forever()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._httpd is not None:
            cls._httpd.shutdown()
        try:
            cls.service.backend.close()
        except Exception:
            pass
        import shutil

        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # -- helpers ------------------------------------------------------

    def _signup(self, email: str, password: str = "password-123", tenant_id: str | None = None) -> dict:
        body: dict = {"email": email, "password": password}
        if tenant_id is not None:
            body["tenant_id"] = tenant_id
        status, resp = _post(self.base, "/v1/auth/signup", body)
        self.assertEqual(status, HTTPStatus.CREATED, f"signup failed: {resp}")
        return resp

    def _mcp_call(self, token: str, name: str, args: dict, request_id: int = 100) -> dict:
        status, payload = _mcp(self.base, "tools/call", {"name": name, "arguments": args},
                               token=token, request_id=request_id)
        self.assertEqual(status, HTTPStatus.OK, f"tools/call {name} HTTP {status}: {payload}")
        result = (payload or {}).get("result") or {}
        if result.get("isError"):
            return {"isError": True, "error": _tool_error_text(result)}
        return {"isError": False, "result": result.get("structuredContent")}

    def _assert_ok(self, token: str, name: str, args: dict, request_id: int = 100) -> dict:
        resp = self._mcp_call(token, name, args, request_id)
        self.assertFalse(resp["isError"], f"{name} failed: {resp}")
        return resp["result"]

    def _assert_is_error(self, token: str, name: str, args: dict, code: str, request_id: int = 100) -> dict:
        resp = self._mcp_call(token, name, args, request_id)
        self.assertTrue(resp["isError"], f"{name} unexpectedly succeeded: {resp}")
        self.assertEqual(resp["error"].get("code"), code, f"{name} error code mismatch: {resp}")
        return resp


class HostedMCPHandshakeTests(HostedMCPTestBase):
    """MCP protocol handshake: initialize, tools/list, notifications, ping."""

    def test_authenticated_initialize_tools_list_and_ping(self) -> None:
        acct = self._signup("handshake@example.com")
        token = acct["session_token"]

        status, init = _mcp(self.base, "initialize",
                            {"protocolVersion": "2025-03-26", "capabilities": {}},
                            token=token, request_id=1)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIn(init["result"]["protocolVersion"], ("2025-11-25", "2024-11-05"))
        self.assertEqual(init["result"]["serverInfo"]["name"], "weft-cloud")

        status, listing = _mcp(self.base, "tools/list", None, token=token, request_id=2)
        self.assertEqual(status, HTTPStatus.OK)
        tool_names = [t["name"] for t in listing["result"]["tools"]]
        self.assertEqual(tool_names, HOSTED_TOOL_NAMES)
        for schema in listing["result"]["tools"]:
            props = schema["inputSchema"]["properties"]
            self.assertNotIn("team_id", props)
            self.assertNotIn("agent_id", props)
            self.assertNotIn("actor_token", props)

        status, pong = _mcp(self.base, "ping", None, token=token, request_id=3)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(pong["result"], {})

    def test_hosted_surface_is_a_small_correct_set(self) -> None:
        """The hosted surface exposes ONLY the room tools, never the full
        self-hosted 58-tool surface (register_agent, pairing, task, …)."""
        acct = self._signup("smallset@example.com")
        token = acct["session_token"]
        _, listing = _mcp(self.base, "tools/list", None, token=token, request_id=1)
        names = [t["name"] for t in listing["result"]["tools"]]
        self.assertEqual(len(names), 8)
        for forbidden in ("register_agent", "create_pairing", "join_pairing",
                          "create_task", "claim_task", "verify_task",
                          "complete_task", "org_create", "roster_create"):
            self.assertNotIn(forbidden, names)

    def test_initialized_notification_is_202(self) -> None:
        acct = self._signup("notify@example.com")
        token = acct["session_token"]
        status, payload = _mcp(self.base, "notifications/initialized", {}, token=token,
                               request_id=None, notification=True)
        self.assertEqual(status, HTTPStatus.ACCEPTED)
        self.assertIsNone(payload)

    def test_get_mcp_is_405(self) -> None:
        req = urllib.request.Request(self.base + "/mcp", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10):
                self.fail("GET /mcp must not return 200")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, HTTPStatus.METHOD_NOT_ALLOWED)


class HostedMCPAuthTests(HostedMCPTestBase):
    """An unauthenticated caller is refused with no information about what exists."""

    def test_unauthenticated_calls_refused_identically(self) -> None:
        self._signup("a-seed@example.com", tenant_id="tenant_seed_1")

        status_missing, body_missing = _mcp(
            self.base, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}, token=None, request_id=7)
        status_bad, body_bad = _mcp(
            self.base, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}},
            token="fss_bogus-token-that-resolves-to-nothing", request_id=7)

        self.assertEqual(status_missing, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(status_bad, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(body_missing, body_bad,
                         "missing-token and bad-token responses must be byte-identical (no oracle)")
        self.assertEqual(body_missing["error"]["code"], -32001)
        self.assertEqual(body_missing["error"]["message"], "Unauthorized")

        # tools/list is equally gated pre-auth: the tool set is not enumerable.
        status_list, body_list = _mcp(self.base, "tools/list", None, token=None, request_id=8)
        self.assertEqual(status_list, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(body_list["error"]["message"], "Unauthorized")

        # A revoked session is refused with the SAME generic error.
        acct = self._signup("revoke@example.com")
        token = acct["session_token"]
        status_ok, _ = _mcp(self.base, "ping", None, token=token, request_id=9)
        self.assertEqual(status_ok, HTTPStatus.OK)
        with self.service.backend.transaction() as tx:
            tx.execute("UPDATE cloud_identity_sessions SET revoked_at = datetime('now') "
                       "WHERE token_hash = ?", (hash_token(token),))
            tx.commit()
        status_revoked, body_revoked = _mcp(self.base, "ping", None, token=token, request_id=10)
        self.assertEqual(status_revoked, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(body_revoked["error"]["message"], "Unauthorized")


class HostedMCPRoomFlowTests(HostedMCPTestBase):
    """Room lifecycle over the hosted MCP endpoint, incl. cross-tenant isolation."""

    def _two_accounts(self, prefix: str) -> tuple[dict, dict]:
        a = self._signup(f"{prefix}-a@example.com")
        b = self._signup(f"{prefix}-b@example.com", tenant_id=a["tenant_id"])
        return a, b

    def test_cross_tenant_isolation_room_id_alone_yields_no_oracle(self) -> None:
        owner_a = self._signup("tenant-a@example.com")
        tenant_b = self._signup("tenant-b@example.com")

        status, room = _post(self.base, "/v1/rooms/create", {"cap": 4, "name": "A-private"},
                             token=owner_a["session_token"])
        self.assertEqual(status, HTTPStatus.CREATED)
        room_id_a = room["room_id"]

        # B knows A's room_id but holds no link, no membership, different tenant.
        real = self._assert_is_error(tenant_b["session_token"], "room_info", {"room_id": room_id_a},
                                     "room_not_found", request_id=1)
        fake = self._assert_is_error(tenant_b["session_token"], "room_info",
                                     {"room_id": "room_" + "0" * 32},
                                     "room_not_found", request_id=2)
        self.assertEqual(real, fake, "a foreign room must look identical to a nonexistent one")

        # Same oracle-sealed behaviour for every member-gated read/mutation tool.
        for name, args in (
            ("room_poll", {"room_id": room_id_a}),
            ("room_ack", {"room_id": room_id_a, "seq": 1}),
            ("room_heartbeat", {"room_id": room_id_a}),
            ("room_send", {"room_id": room_id_a, "target_spec": "*", "payload": {"k": "v"}}),
            ("room_event_log", {"room_id": room_id_a}),
        ):
            self._assert_is_error(tenant_b["session_token"], name, args, "room_not_found",
                                  request_id=hash(name) % 1000 + 3)

        # Join without a valid link: identical whether the room exists or not.
        join_real = self._assert_is_error(tenant_b["session_token"], "room_join",
                                          {"room_id": room_id_a, "link_token": "rm_bogus-join-capability",
                                           "consent": True}, "invalid_link", request_id=11)
        join_fake = self._assert_is_error(tenant_b["session_token"], "room_join",
                                          {"room_id": "room_" + "1" * 32, "link_token": "rm_bogus-join-capability",
                                           "consent": True}, "invalid_link", request_id=12)
        self.assertEqual(join_real, join_fake)

    def test_room_created_via_mcp_is_the_same_room_as_v1(self) -> None:
        a, b = self._two_accounts("same")

        # Direction 1: create via /v1, join + message via hosted MCP.
        status, room = _post(self.base, "/v1/rooms/create", {"cap": 4, "name": "v1-made"},
                             token=a["session_token"])
        self.assertEqual(status, HTTPStatus.CREATED)
        joined = self._assert_ok(b["session_token"], "room_join",
                                 {"room_id": room["room_id"], "link_token": room["link_token"],
                                  "consent": True}, request_id=1)
        self.assertEqual(joined["status"], "active")
        self.assertEqual(joined["room_id"], room["room_id"])
        sent = self._assert_ok(b["session_token"], "room_send",
                               {"room_id": room["room_id"], "target_spec": "*",
                                "payload": {"kind": "message", "text": "hello via mcp"}}, request_id=2)
        self.assertTrue(sent["receipts"])

        status, poll = _post(self.base, "/v1/rooms/poll",
                             {"room_id": room["room_id"], "agent_id": a["account_id"]},
                             token=a["session_token"])
        self.assertEqual(status, HTTPStatus.OK)
        messages = [e for e in poll["events"]
                    if e["kind"] == "room.message" and e["origin_agent"] == b["account_id"]]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"]["payload"]["text"], "hello via mcp")

        # Direction 2: create via hosted MCP, join + message via /v1.
        created = self._assert_ok(a["session_token"], "room_create",
                                  {"cap": 4, "name": "mcp-made"}, request_id=3)
        status, joined_v1 = _post(self.base, "/v1/rooms/join",
                                  {"room_id": created["room_id"], "link_token": created["link_token"],
                                   "agent_id": b["account_id"], "consent": True},
                                  token=b["session_token"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(joined_v1["room_id"], created["room_id"])
        status, _ = _post(self.base, "/v1/rooms/send",
                          {"room_id": created["room_id"], "sender_agent_id": b["account_id"],
                           "target_spec": "*", "payload": {"kind": "message", "text": "hello via v1"}},
                          token=b["session_token"])
        self.assertEqual(status, HTTPStatus.OK)

        polled = self._assert_ok(a["session_token"], "room_poll",
                                 {"room_id": created["room_id"]}, request_id=4)
        texts = [e["payload"]["payload"]["text"] for e in polled["events"]
                 if e["kind"] == "room.message"]
        self.assertIn("hello via v1", texts)

    def test_plan_room_member_cap_enforced_through_hosted_mcp(self) -> None:
        a, b = self._two_accounts("cap")
        c = self._signup("cap-c@example.com", tenant_id=a["tenant_id"])
        d = self._signup("cap-d@example.com", tenant_id=a["tenant_id"])

        created = self._assert_ok(a["session_token"], "room_create", {"cap": 3, "name": "quota"}, request_id=1)
        for member in (b, c):
            joined = self._assert_ok(member["session_token"], "room_join",
                                     {"room_id": created["room_id"], "link_token": created["link_token"],
                                      "consent": True}, request_id=2)
            self.assertEqual(joined["status"], "active")

        # The (cap)th join over the hosted MCP endpoint is refused.
        refused = self._assert_is_error(d["session_token"], "room_join",
                                        {"room_id": created["room_id"], "link_token": created["link_token"],
                                         "consent": True}, "room_full", request_id=3)
        self.assertEqual(refused["error"]["code"], "room_full")

        info = self._assert_ok(a["session_token"], "room_info", {"room_id": created["room_id"]}, request_id=4)
        self.assertEqual(info["member_count"], 3)

    def test_identity_arguments_are_rejected(self) -> None:
        a = self._signup("identity@example.com")
        token = a["session_token"]

        for bad_args in (
            {"cap": 4, "agent_id": a["account_id"]},
            {"cap": 4, "actor_token": "fst_actor_attacker"},
            {"cap": 4, "team_id": "any-team"},
        ):
            resp = self._assert_is_error(token, "room_create", bad_args, "invalid_argument",
                                         request_id=1)
            self.assertIn("not accepted", resp["error"]["message"])

        join_bad = self._assert_is_error(token, "room_join",
                                         {"room_id": "room_x", "link_token": "rm_x", "consent": True,
                                          "sender_agent_id": "someone-else"},
                                         "invalid_argument", request_id=2)
        self.assertIn("not accepted", join_bad["error"]["message"])

    def test_full_mcp_lifecycle_with_ack_and_presence(self) -> None:
        a, b = self._two_accounts("life")
        created = self._assert_ok(a["session_token"], "room_create", {"cap": 4}, request_id=1)
        self._assert_ok(b["session_token"], "room_join",
                        {"room_id": created["room_id"], "link_token": created["link_token"],
                         "consent": True}, request_id=2)
        self._assert_ok(a["session_token"], "room_send",
                        {"room_id": created["room_id"], "target_spec": "*",
                         "payload": {"text": "one"}}, request_id=3)

        polled = self._assert_ok(b["session_token"], "room_poll",
                                 {"room_id": created["room_id"]}, request_id=4)
        events = polled["events"]
        self.assertTrue(any(e["kind"] == "room.message" for e in events))
        head = polled["cursor_head"]
        self._assert_ok(b["session_token"], "room_ack", {"room_id": created["room_id"], "seq": head},
                        request_id=5)
        acked = self._assert_ok(b["session_token"], "room_poll",
                                {"room_id": created["room_id"]}, request_id=6)
        self.assertEqual(acked["events"], [], "acked events must not be re-delivered")

        self._assert_ok(b["session_token"], "room_heartbeat", {"room_id": created["room_id"]},
                        request_id=7)
        info = self._assert_ok(a["session_token"], "room_info", {"room_id": created["room_id"]},
                               request_id=8)
        self.assertEqual(info["member_count"], 2)
        by_id = {m["agent_id"]: m for m in info["members"]}
        self.assertIn(b["account_id"], by_id)
        self.assertEqual(by_id[b["account_id"]]["status"], "active")


if __name__ == "__main__":
    unittest.main()
