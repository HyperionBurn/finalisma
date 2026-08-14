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
import time
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.identity.tokens import hash_token
from weft_cloud.mcp import MAX_JSON_RPC_BYTES
from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend

HOSTED_TOOL_NAMES = [
    "room_create",
    "room_join",
    "room_send",
    "room_receipts",
    "room_poll",
    "room_wait",
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
        finally:
            exc.close()  # release the unread response body / socket
        return exc.code, payload


def _mcp(base: str, method: str, params: dict | None, token: str | None = None,
         request_id: int | None = 1, notification: bool = False,
         timeout: int = 10) -> tuple[int, dict | None]:
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
            exc.close()  # release the unread response body / socket
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
        from http.server import ThreadingHTTPServer

        # Bind an ephemeral port (0) and read back the actual port so no two
        # test classes ever contend for a fixed address. Binding happens
        # synchronously in setUpClass, so a bind failure raises here as a real
        # error instead of silently killing a background thread.
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
        # shutdown() stops serve_forever but does NOT close the listening
        # socket; server_close() is what releases the port. Both must run even
        # when a test failed, or a leaked listener can hijack a later test's
        # connections (they handshake then hang -> TimeoutError).
        if cls._httpd is not None:
            try:
                cls._httpd.shutdown()
            finally:
                cls._httpd.server_close()
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

    def _assert_is_error(self, token: str, name: str, args: dict, code: str, request_id: int = 100) -> dict:
        resp = self._mcp_call(token, name, args, request_id)
        self.assertTrue(resp["isError"], f"{name} unexpectedly succeeded: {resp}")
        self.assertEqual(resp["error"].get("code"), code, f"{name} error code mismatch: {resp}")
        return resp


class HostedMCPHandshakeTests(HostedMCPTestBase):
    """MCP protocol handshake: initialize, tools/list, notifications, ping."""

    def test_oversized_mcp_body_returns_structured_limit_error(self) -> None:
        acct = self._signup("oversized-mcp@example.com")
        request = urllib.request.Request(
            self.base + "/mcp",
            data=b"x" * (MAX_JSON_RPC_BYTES + 1),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {acct['session_token']}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                self.fail(f"oversized MCP body unexpectedly returned {response.status}")
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

        self.assertEqual(status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertIsNone(body["id"])
        self.assertEqual(body["error"]["code"], -32600)
        self.assertEqual(body["error"]["data"]["code"], "request_too_large")
        self.assertEqual(body["error"]["data"]["max_bytes"], MAX_JSON_RPC_BYTES)
        self.assertIn(str(MAX_JSON_RPC_BYTES), body["error"]["message"])

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
        self.assertEqual(len(names), 10)
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
            try:
                self.assertEqual(exc.code, HTTPStatus.METHOD_NOT_ALLOWED)
            finally:
                exc.close()  # release the unread response body / socket


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
            ("room_receipts", {"room_id": room_id_a, "entry_ids": []}),
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
                             {"room_id": room["room_id"]},
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
                                   "consent": True},
                                  token=b["session_token"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(joined_v1["room_id"], created["room_id"])
        status, _ = _post(self.base, "/v1/rooms/send",
                          {"room_id": created["room_id"],
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

    def test_over_limit_cap_is_quota_exceeded_across_both_surfaces(self) -> None:
        """A cap above the free plan's member limit must surface as
        quota_exceeded (never internal_error) on the hosted MCP surface, with
        the SAME code and plan-naming message the /v1 REST surface returns.
        The message (max 10 members per room, free plan) is what lets an agent
        lower the cap and retry instead of assuming the product crashed."""
        a = self._signup("quota-cap@example.com")

        # MCP surface: cap=17 is above the free plan's 10-member room limit.
        mcp = self._mcp_call(a["session_token"], "room_create",
                             {"cap": 17, "name": "over-limit"}, request_id=1)
        self.assertTrue(mcp["isError"], f"over-limit cap must be an error: {mcp}")
        self.assertEqual(mcp["error"]["code"], "quota_exceeded")
        self.assertNotEqual(mcp["error"]["code"], "internal_error",
                            "quota must not masquerade as an internal failure")
        self.assertIn("max 10 members per room", mcp["error"]["message"])
        self.assertIn("free plan", mcp["error"]["message"])
        self.assertEqual(mcp["error"]["limit"]["name"], "max_members_per_room")
        self.assertEqual(mcp["error"]["limit"]["value"], 10)
        self.assertEqual(mcp["error"]["limit"]["plan"], "free")

        # REST surface: the same condition yields the same code + limit detail.
        status, rest = _post(self.base, "/v1/rooms/create", {"cap": 17},
                             token=a["session_token"])
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(rest["error"]["code"], "quota_exceeded")
        self.assertIn("max 10 members per room", rest["error"]["message"])
        self.assertEqual(rest["error"]["limit"], mcp["error"]["limit"],
                         "the two surfaces must agree on the limit detail")

        # Both surfaces still accept a cap within the plan limit.
        within_mcp = self._assert_ok(a["session_token"], "room_create",
                                     {"cap": 8, "name": "within"}, request_id=2)
        self.assertIn("room_id", within_mcp)
        self.assertEqual(within_mcp["cap"], 8)
        status, within_rest = _post(self.base, "/v1/rooms/create", {"cap": 8},
                                    token=a["session_token"])
        self.assertEqual(status, HTTPStatus.CREATED)

    def test_cross_tenant_link_join_grants_membership_not_privilege(self) -> None:
        """A link is the cross-tenant capability (as on /v1): tenant B may
        join A's room with the link and then operate as a member, while a
        third tenant with no link and no membership stays sealed."""
        owner_a = self._signup("link-owner@example.com")
        tenant_b = self._signup("link-joiner@example.com")
        tenant_c = self._signup("link-outsider@example.com")

        status, room = _post(self.base, "/v1/rooms/create", {"cap": 4}, token=owner_a["session_token"])
        self.assertEqual(status, HTTPStatus.CREATED)

        joined = self._assert_ok(tenant_b["session_token"], "room_join",
                                 {"room_id": room["room_id"], "link_token": room["link_token"],
                                  "consent": True}, request_id=1)
        self.assertEqual(joined["status"], "active")

        sent = self._assert_ok(tenant_b["session_token"], "room_send",
                               {"room_id": room["room_id"], "target_spec": "*",
                                "payload": {"text": "from another tenant"}}, request_id=2)
        self.assertGreater(sent["seq"], 0)
        polled = self._assert_ok(tenant_b["session_token"], "room_poll",
                                 {"room_id": room["room_id"]}, request_id=3)
        self.assertTrue(any(e["kind"] == "room.message" for e in polled["events"]))

        # Tenant C: no link, no membership -> identical to nonexistent room.
        outsider = self._assert_is_error(tenant_c["session_token"], "room_info",
                                         {"room_id": room["room_id"]}, "room_not_found", request_id=4)
        nonexistent = self._assert_is_error(tenant_c["session_token"], "room_info",
                                            {"room_id": "room_" + "f" * 32}, "room_not_found", request_id=5)
        self.assertEqual(outsider, nonexistent)

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

    def test_sender_receipts_show_queued_then_read_without_cross_sender_leak(self) -> None:
        """Receipt state is queryable over the real hosted MCP customer path."""
        a, b = self._two_accounts("mcp-receipts")
        created = self._assert_ok(a["session_token"], "room_create", {"cap": 4}, request_id=1)
        self._assert_ok(b["session_token"], "room_join", {
            "room_id": created["room_id"],
            "link_token": created["link_token"],
            "consent": True,
        }, request_id=2)

        sent = self._assert_ok(a["session_token"], "room_send", {
            "room_id": created["room_id"],
            "target_spec": b["account_id"],
            "payload": {"text": "receipt-me"},
        }, request_id=3)
        entry_id = sent["receipts"][0]["entry_id"]

        before = self._assert_ok(a["session_token"], "room_receipts", {
            "room_id": created["room_id"],
            "entry_ids": [entry_id],
        }, request_id=4)
        self.assertEqual(before["receipts"][0]["status"], "queued")
        self.assertEqual(before["receipts"][0]["read_status"], "queued")

        recipient_poll = self._assert_ok(b["session_token"], "room_poll", {
            "room_id": created["room_id"],
            "after_seq": 0,
        }, request_id=5)
        self.assertTrue(any(event["seq"] == sent["seq"] for event in recipient_poll["events"]))
        self._assert_ok(b["session_token"], "room_ack", {
            "room_id": created["room_id"],
            "seq": sent["seq"],
        }, request_id=6)

        recipient_view = self._assert_ok(b["session_token"], "room_receipts", {
            "room_id": created["room_id"],
            "entry_ids": [entry_id],
        }, request_id=7)
        self.assertEqual(recipient_view["receipts"][0]["status"], "not_found")
        self.assertEqual(recipient_view["receipts"][0]["read_status"], "unknown")

        after = self._assert_ok(a["session_token"], "room_receipts", {
            "room_id": created["room_id"],
            "entry_ids": [entry_id, "oeb_unknown-receipt"],
        }, request_id=8)
        by_id = {receipt["entry_id"]: receipt for receipt in after["receipts"]}
        self.assertEqual(by_id[entry_id]["read_status"], "read")
        self.assertEqual(by_id["oeb_unknown-receipt"]["status"], "not_found")
        self.assertEqual(by_id["oeb_unknown-receipt"]["read_status"], "unknown")


class HostedMCPIdempotencyTests(HostedMCPTestBase):
    """``room_send`` idempotency_key validation — identical to the REST surface.

    The hosted MCP ``room_send`` tool and REST ``/v1/rooms/send`` share the
    same ``CloudRoomService.room_send`` implementation, so a malformed
    idempotency_key must refuse with the SAME ``invalid_argument`` error here
    as it does over REST — never a server error. A list or dict key used to
    crash the handler (500) on production; int/bool were silently accepted.
    """

    MAX_LEN = 256

    def _owner_room(self, prefix: str) -> tuple[dict, dict]:
        acct = self._signup(f"{prefix}@example.com")
        room = self._assert_ok(acct["session_token"], "room_create", {"cap": 4}, request_id=1)
        return acct, room

    def _assert_room_send_invalid_argument(self, token: str, room_id: str, key: Any,
                                           request_id: int) -> None:
        resp = self._mcp_call(token, "room_send", {
            "room_id": room_id,
            "target_spec": "*",
            "payload": {"text": "x"},
            "idempotency_key": key,
        }, request_id=request_id)
        self.assertTrue(resp["isError"], f"key {key!r} unexpectedly accepted: {resp}")
        self.assertEqual(resp["error"].get("code"), "invalid_argument",
                         f"key {key!r} must be invalid_argument, got: {resp}")
        self.assertNotEqual(resp["error"].get("code"), "internal_error",
                            f"key {key!r} must never surface as a server error: {resp}")

    def test_non_string_types_are_invalid_argument_not_server_error(self) -> None:
        acct, room = self._owner_room("mcp-idemval-types")
        for i, key in enumerate((["a", "b"], {"k": "v"}, 12345, True, 1.5)):
            self._assert_room_send_invalid_argument(acct["session_token"], room["room_id"], key,
                                                    request_id=10 + i)

    def test_empty_and_whitespace_only_strings_are_rejected(self) -> None:
        acct, room = self._owner_room("mcp-idemval-blank")
        for i, key in enumerate(("", "   ", "\t\n")):
            self._assert_room_send_invalid_argument(acct["session_token"], room["room_id"], key,
                                                    request_id=20 + i)

    def test_over_length_key_is_rejected(self) -> None:
        acct, room = self._owner_room("mcp-idemval-len")
        self._assert_room_send_invalid_argument(acct["session_token"], room["room_id"],
                                                "k" * (self.MAX_LEN + 1), request_id=31)

    def test_absent_or_null_key_is_still_accepted(self) -> None:
        acct, room = self._owner_room("mcp-idemval-null")
        for i, args in enumerate(({"room_id": room["room_id"], "target_spec": "*",
                                   "payload": {"text": "a"}},
                                  {"room_id": room["room_id"], "target_spec": "*",
                                   "payload": {"text": "b"}, "idempotency_key": None})):
            resp = self._assert_ok(acct["session_token"], "room_send", args, request_id=40 + i)
            self.assertIn("seq", resp)

    def test_valid_key_reused_returns_same_seq_no_duplicate(self) -> None:
        acct, room = self._owner_room("mcp-idemval-reuse")
        args = {"room_id": room["room_id"], "target_spec": "*", "payload": {"text": "once"},
                "idempotency_key": "mcp-idem-1"}
        first = self._assert_ok(acct["session_token"], "room_send", args, request_id=51)
        second = self._assert_ok(acct["session_token"], "room_send", args, request_id=52)
        self.assertEqual(second["seq"], first["seq"],
                         "same key must return the SAME seq over the hosted MCP path")
        log = self._assert_ok(acct["session_token"], "room_event_log",
                              {"room_id": room["room_id"]}, request_id=53)
        messages = [e for e in log["events"] if e["kind"] == "room.message"]
        self.assertEqual(len(messages), 1, "same key twice must not create a duplicate event")

    def test_error_never_echoes_the_raw_key_value(self) -> None:
        acct, room = self._owner_room("mcp-idemval-noleak")
        secret_key = "k" * 300
        self._assert_room_send_invalid_argument(acct["session_token"], room["room_id"],
                                                secret_key, request_id=61)
        # Assert the raw key is absent from the rendered error surface too.
        status, payload = _mcp(self.base, "tools/call",
                               {"name": "room_send", "arguments": {
                                   "room_id": room["room_id"], "target_spec": "*",
                                   "payload": {"text": "x"}, "idempotency_key": secret_key}},
                               token=acct["session_token"], request_id=62)
        self.assertEqual(status, HTTPStatus.OK)
        text = json.dumps(payload)
        self.assertNotIn(secret_key, text,
                         "the raw idempotency_key must not be echoed into the MCP error")


class HostedMCPRoomWaitTests(HostedMCPTestBase):
    """``room_wait`` — the blocking long-poll that keeps agents IN their turn.

    Every test drives the real hosted MCP endpoint over real HTTP. Wait
    durations are asserted against wall-clock time so the suite proves
    "woke on the event" (elapsed well under the timeout) rather than
    "returned when the clock ran out".
    """

    def _room_head(self, token: str, room_id: str) -> int:
        """Current room cursor_head (the last committed seq) for the caller."""
        polled = self._assert_ok(token, "room_poll", {"room_id": room_id})
        return polled["cursor_head"]

    def _two_accounts(self, prefix: str) -> tuple[dict, dict]:
        a = self._signup(f"{prefix}-a@example.com")
        b = self._signup(f"{prefix}-b@example.com", tenant_id=a["tenant_id"])
        return a, b

    def _wait_async(self, token: str, args: dict, request_id: int) -> tuple[dict, threading.Thread]:
        """Call room_wait in a daemon thread; return (holder, thread).

        ``holder`` collects either ``{"resp": <mcp result>}`` or
        ``{"error": <exception>}`` so a worker-thread failure is visible to
        the main thread instead of silently killing the request.
        """
        holder: dict = {}

        def _run() -> None:
            try:
                holder["resp"] = self._mcp_call(token, "room_wait", args, request_id, timeout=30)
            except Exception as exc:  # pragma: no cover - defensive
                holder["error"] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return holder, thread

    def _assert_wait_ok(self, holder: dict, thread: threading.Thread,
                        timeout: float = 25.0) -> dict:
        thread.join(timeout=timeout)
        self.assertFalse(thread.is_alive(), "room_wait never returned")
        self.assertNotIn("error", holder, f"room_wait raised: {holder.get('error')}")
        resp = holder.get("resp")
        self.assertIsNotNone(resp, "room_wait produced no response")
        self.assertFalse(resp["isError"], f"room_wait returned an error: {resp}")
        return resp["result"]

    def test_returns_immediately_when_event_already_available(self) -> None:
        """An event already present after after_seq must return with NO delay."""
        a, b = self._two_accounts("immed")
        created = self._assert_ok(a["session_token"], "room_create", {"cap": 4}, request_id=1)
        self._assert_ok(b["session_token"], "room_join",
                        {"room_id": created["room_id"], "link_token": created["link_token"],
                         "consent": True}, request_id=2)
        # b has pending events (the create + join) already; wait from seq 0.
        start = time.monotonic()
        result = self._assert_ok(b["session_token"], "room_wait",
                                 {"room_id": created["room_id"], "after_seq": 0,
                                  "timeout_seconds": 10}, request_id=3)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0, f"room_wait blocked {elapsed:.2f}s with events ready")
        self.assertFalse(result["timed_out"])
        self.assertTrue(result["events"], "expected the already-present events back")

    def test_blocks_then_wakes_promptly_on_new_event(self) -> None:
        """A blocked waiter must wake on the event, well under the timeout."""
        a, b = self._two_accounts("wake")
        created = self._assert_ok(a["session_token"], "room_create", {"cap": 4}, request_id=1)
        self._assert_ok(b["session_token"], "room_join",
                        {"room_id": created["room_id"], "link_token": created["link_token"],
                         "consent": True}, request_id=2)
        head = self._room_head(b["session_token"], created["room_id"])
        self._assert_ok(b["session_token"], "room_ack", {"room_id": created["room_id"], "seq": head},
                        request_id=3)

        holder, thread = self._wait_async(
            b["session_token"],
            {"room_id": created["room_id"], "after_seq": head, "timeout_seconds": 10},
            request_id=4)
        time.sleep(0.6)  # let b get blocked inside the wait loop
        start = time.monotonic()
        self._assert_ok(a["session_token"], "room_send",
                        {"room_id": created["room_id"], "target_spec": "*",
                         "payload": {"kind": "message", "text": "wake up"}}, request_id=5)
        result = self._assert_wait_ok(holder, thread)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 4.0,
                        f"woke on the clock, not the event ({elapsed:.2f}s vs 10s timeout)")
        self.assertFalse(result["timed_out"])
        texts = [e["payload"]["payload"]["text"] for e in result["events"]
                 if e["kind"] == "room.message"]
        self.assertIn("wake up", texts)

    def test_returns_empty_not_error_at_timeout(self) -> None:
        """Nobody speaks: EMPTY result at the deadline, not an error."""
        a, b = self._two_accounts("timed")
        created = self._assert_ok(a["session_token"], "room_create", {"cap": 4}, request_id=1)
        self._assert_ok(b["session_token"], "room_join",
                        {"room_id": created["room_id"], "link_token": created["link_token"],
                         "consent": True}, request_id=2)
        head = self._room_head(b["session_token"], created["room_id"])
        self._assert_ok(b["session_token"], "room_ack", {"room_id": created["room_id"], "seq": head},
                        request_id=3)

        start = time.monotonic()
        result = self._assert_ok(b["session_token"], "room_wait",
                                 {"room_id": created["room_id"], "after_seq": head,
                                  "timeout_seconds": 2}, request_id=4)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 1.5, f"returned early ({elapsed:.2f}s for a 2s timeout)")
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["events"], [], "timeout must return an EMPTY event list")
        self.assertEqual(result["next_seq"], head, "next_seq must stay pinned at after_seq")

    def test_non_addressee_waiting_on_unicast_gets_redacted_envelope(self) -> None:
        """A blocking read must not become a way around confidentiality."""
        a = self._signup("redact-o@example.com")
        b = self._signup("redact-b@example.com", tenant_id=a["tenant_id"])
        c = self._signup("redact-c@example.com", tenant_id=a["tenant_id"])
        created = self._assert_ok(a["session_token"], "room_create", {"cap": 4}, request_id=1)
        for member in (b, c):
            self._assert_ok(member["session_token"], "room_join",
                            {"room_id": created["room_id"], "link_token": created["link_token"],
                             "consent": True}, request_id=2)
        head = self._room_head(c["session_token"], created["room_id"])
        self._assert_ok(c["session_token"], "room_ack", {"room_id": created["room_id"], "seq": head},
                        request_id=3)
        self._assert_ok(b["session_token"], "room_ack", {"room_id": created["room_id"], "seq": head},
                        request_id=4)

        # a unicasts to b only; c is a member but NOT the addressee.
        self._assert_ok(a["session_token"], "room_send",
                        {"room_id": created["room_id"], "target_spec": b["account_id"],
                         "payload": {"kind": "secret", "text": "for-b-eyes-only"}}, request_id=5)

        b_result = self._assert_ok(b["session_token"], "room_wait",
                                   {"room_id": created["room_id"], "after_seq": head,
                                    "timeout_seconds": 3}, request_id=6)
        c_result = self._assert_ok(c["session_token"], "room_wait",
                                   {"room_id": created["room_id"], "after_seq": head,
                                    "timeout_seconds": 3}, request_id=7)
        b_msg = [e for e in b_result["events"] if e["kind"] == "room.message"][0]
        c_msg = [e for e in c_result["events"] if e["kind"] == "room.message"][0]
        self.assertEqual(b_msg["payload"]["payload"]["text"], "for-b-eyes-only")
        self.assertEqual(c_msg["payload"], {"redacted": True, "reason": "not_the_addressee"})
        # Both see the SAME envelope sequence position, never the body for c.
        self.assertEqual(b_msg["seq"], c_msg["seq"])

    def test_rejects_smuggled_identity_arguments(self) -> None:
        """agent_id / sender_agent_id / tenant_id must be refused on room_wait."""
        a = self._signup("smuggle@example.com")
        token = a["session_token"]
        created = self._assert_ok(token, "room_create", {"cap": 4}, request_id=1)
        for bad_args in (
            {"room_id": created["room_id"], "agent_id": a["account_id"]},
            {"room_id": created["room_id"], "sender_agent_id": "someone-else"},
            {"room_id": created["room_id"], "tenant_id": "tenant-evil"},
        ):
            resp = self._assert_is_error(token, "room_wait", bad_args, "invalid_argument",
                                         request_id=2)
            self.assertIn("not accepted", resp["error"]["message"])

    def test_concurrent_waiters_one_sender_all_wake_and_agree_on_ordering(self) -> None:
        """The multi-agent case: 4 waiters, 1 sender, every waiter wakes and
        every waiter's events are the same ordered prefix of the broadcast."""
        a = self._signup("conc-o@example.com")
        waiters = [self._signup(f"conc-w{i}@example.com", tenant_id=a["tenant_id"])
                   for i in range(4)]
        created = self._assert_ok(a["session_token"], "room_create", {"cap": 8}, request_id=1)
        for w in waiters:
            self._assert_ok(w["session_token"], "room_join",
                            {"room_id": created["room_id"], "link_token": created["link_token"],
                             "consent": True}, request_id=2)
        head = self._room_head(waiters[0]["session_token"], created["room_id"])
        for w in waiters:
            self._assert_ok(w["session_token"], "room_ack",
                            {"room_id": created["room_id"], "seq": head}, request_id=3)

        holders: list[dict] = []
        threads: list[threading.Thread] = []
        for i in range(4):
            holder, thread = self._wait_async(
                waiters[i]["session_token"],
                {"room_id": created["room_id"], "after_seq": head, "timeout_seconds": 12},
                request_id=10 + i)
            holders.append(holder)
            threads.append(thread)

        time.sleep(0.7)  # let all four block inside their wait loops
        for text in ("first", "second", "third"):
            self._assert_ok(a["session_token"], "room_send",
                            {"room_id": created["room_id"], "target_spec": "*",
                             "payload": {"kind": "message", "text": text}}, request_id=20 + len(text))

        start = time.monotonic()
        results = [self._assert_wait_ok(h, t) for h, t in zip(holders, threads)]
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 8.0, f"waiters woke on the clock: {elapsed:.2f}s")

        expected = [head + 1, head + 2, head + 3]
        for i, result in enumerate(results):
            self.assertFalse(result["timed_out"], f"waiter {i} timed out and never woke")
            seqs = [e["seq"] for e in result["events"]]
            self.assertTrue(seqs, f"waiter {i} woke with no events")
            self.assertEqual(seqs, expected[:len(seqs)],
                             f"waiter {i} saw divergent ordering: {seqs} vs {expected}")

    def test_blocked_waiter_does_not_block_writers(self) -> None:
        """A blocking read must not hold the SQLite writer lock: a send must
        complete while another agent is blocked inside room_wait."""
        a, b = self._two_accounts("nofreeze")
        created = self._assert_ok(a["session_token"], "room_create", {"cap": 4}, request_id=1)
        self._assert_ok(b["session_token"], "room_join",
                        {"room_id": created["room_id"], "link_token": created["link_token"],
                         "consent": True}, request_id=2)
        head = self._room_head(b["session_token"], created["room_id"])
        self._assert_ok(b["session_token"], "room_ack", {"room_id": created["room_id"], "seq": head},
                        request_id=3)

        holder, thread = self._wait_async(
            b["session_token"],
            {"room_id": created["room_id"], "after_seq": head, "timeout_seconds": 10},
            request_id=4)
        time.sleep(0.6)  # b is now blocked inside the wait loop
        start = time.monotonic()
        sent = self._assert_ok(a["session_token"], "room_send",
                               {"room_id": created["room_id"], "target_spec": "*",
                                "payload": {"kind": "message", "text": "still alive"}}, request_id=5)
        write_elapsed = time.monotonic() - start
        self.assertLess(write_elapsed, 2.0,
                        f"writer starved by the blocked waiter ({write_elapsed:.2f}s)")
        self.assertTrue(sent["receipts"])

        result = self._assert_wait_ok(holder, thread)
        self.assertFalse(result["timed_out"])
        texts = [e["payload"]["payload"]["text"] for e in result["events"]
                 if e["kind"] == "room.message"]
        self.assertIn("still alive", texts)


if __name__ == "__main__":
    unittest.main()
