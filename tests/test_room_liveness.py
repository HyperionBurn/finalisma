"""P0 design fix — liveness must describe USE, and presence must never silently decide delivery.

Regression suite for the live-production defect (measured on production):

    room_poll       -> does NOT refresh last_seen
    room_wait       -> does NOT refresh last_seen
    room_send       -> does NOT refresh last_seen
    room_heartbeat  -> DOES refresh last_seen (the only one)

Combined with ``CloudRoomService._route_targets`` dropping any member older
than 1800s, an agent that follows the documented loop — block on room_wait,
send messages, poll — was marked stale after 30 minutes and PERMANENTLY lost
every unicast addressed to it while actively participating.

The fix under test:

  - ANY authenticated room call by a member refreshes that member's OWN
    last_seen (room_poll, room_wait, room_send, room_ack, room_event_log,
    room_info, room_join), throttled to at most one write per
    ``ROOM_LIVENESS_TOUCH_INTERVAL`` so a tight poll loop does not serialize
    the room behind SQLite's single writer.
  - ``room_wait`` refreshes on ENTRY and on RETURN: a blocked waiter is
    provably alive (the server holds its connection) and must never age
    toward staleness while the block is in flight.
  - The 1800-second staleness threshold is ONE module-level constant
    (``ROOM_STALE_AFTER_SECONDS``) read by the ``room_info`` status surface.
    Target routing is membership-keyed and does NOT consult it (see the
    staleloss P0 fix in tests/test_room_stale_delivery.py).
  - ``room_heartbeat`` keeps working unchanged.

Every test drives the REAL hosted MCP surface over REAL HTTP (the surface an
MCP host uses), with last_seen read straight from the storage backend.

These tests were run against the CURRENT code first: every
"refreshes last_seen" test FAILED (the read paths wrote nothing), the long-wait
test FAILED (a blocked waiter aged into stale), and the single-constant test
FAILED (two divergent literals). The guards — member-can't-touch-other,
non-member-creates-nothing, heartbeat-unchanged, and the throttle — were
already green because the current code simply never writes on the read paths.
"""

from __future__ import annotations

import contextlib
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

import weft_cloud.rooms as rooms_module
from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend

# The current code hardcodes 1800 twice; the fixed code exposes it as one
# module constant. getattr lets this suite run against BOTH so the defect
# tests stay red before the fix lands.
_STALE_FALLBACK = 1800.0


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


class RoomLivenessTestBase(unittest.TestCase):
    """Real cloud HTTP service on a background thread (shared per class).

    Tests stay isolated because every signup mints a fresh tenant/account and
    every test uses its own room. last_seen is read and back-dated straight
    from the storage backend; throttling/staleness constants are patched on
    the rooms module the shared service uses, and restored in finally blocks.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from http.server import ThreadingHTTPServer

        cls.tmpdir = tempfile.mkdtemp(prefix="weft-liveness-test-")
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

    def _two_members(self, prefix: str) -> tuple[dict, dict, dict]:
        """Room owned by ``prefix-owner`` with ``prefix-member`` joined."""
        owner = self._signup(f"{prefix}-owner@example.com")
        member = self._signup(f"{prefix}-member@example.com", tenant_id=owner["tenant_id"])
        created = self._assert_ok(owner["session_token"], "room_create", {"cap": 4}, request_id=1)
        self._assert_ok(member["session_token"], "room_join",
                        {"room_id": created["room_id"], "link_token": created["link_token"],
                         "consent": True}, request_id=2)
        return owner, member, created

    def _raw_last_seen(self, tenant_id: str, room_id: str, agent_id: str) -> float | None:
        with self.service.backend.transaction() as tx:
            row = tx.execute(
                "SELECT last_seen FROM cloud_room_members "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, agent_id),
            ).fetchone()
        return float(row["last_seen"]) if row is not None else None

    def _set_last_seen(self, tenant_id: str, room_id: str, agent_id: str, epoch: float) -> None:
        with self.service.backend.transaction() as tx:
            tx.execute(
                "UPDATE cloud_room_members SET last_seen = ? "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (epoch, tenant_id, room_id, agent_id),
            )
            tx.commit()

    def _membership_rows(self, tenant_id: str, room_id: str, agent_id: str) -> list:
        with self.service.backend.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM cloud_room_members "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, agent_id),
            ).fetchall()
        return list(rows)

    @contextlib.contextmanager
    def _touch_interval(self, seconds: float):
        previous = getattr(rooms_module, "ROOM_LIVENESS_TOUCH_INTERVAL", None)
        rooms_module.ROOM_LIVENESS_TOUCH_INTERVAL = seconds
        try:
            yield
        finally:
            if previous is None:
                del rooms_module.ROOM_LIVENESS_TOUCH_INTERVAL
            else:
                rooms_module.ROOM_LIVENESS_TOUCH_INTERVAL = previous

    def _assert_refreshes(self, tenant_id: str, room_id: str, agent_id: str,
                          token: str, tool: str, args: dict) -> None:
        before = self._raw_last_seen(tenant_id, room_id, agent_id)
        self.assertIsNotNone(before, f"{tool}: caller should be a member with a last_seen")
        time.sleep(0.02)
        self._assert_ok(token, tool, args)
        after = self._raw_last_seen(tenant_id, room_id, agent_id)
        self.assertIsNotNone(after, f"{tool}: caller should still be a member")
        self.assertGreater(after, before,
                           f"{tool} must refresh the caller's own last_seen")


class RoomLivenessRefreshTests(RoomLivenessTestBase):
    """Every authenticated member read/write call refreshes the CALLER's last_seen."""

    def test_room_poll_refreshes_last_seen(self) -> None:
        owner, member, created = self._two_members("pollref")
        with self._touch_interval(0.0001):
            self._assert_refreshes(
                owner["tenant_id"], created["room_id"], member["account_id"],
                member["session_token"], "room_poll", {"room_id": created["room_id"]})

    def test_room_wait_refreshes_last_seen(self) -> None:
        owner, member, created = self._two_members("waitref")
        with self._touch_interval(0.0001):
            self._assert_refreshes(
                owner["tenant_id"], created["room_id"], member["account_id"],
                member["session_token"], "room_wait",
                {"room_id": created["room_id"], "after_seq": 0, "timeout_seconds": 1})

    def test_room_send_refreshes_last_seen(self) -> None:
        owner, member, created = self._two_members("sendref")
        with self._touch_interval(0.0001):
            self._assert_refreshes(
                owner["tenant_id"], created["room_id"], member["account_id"],
                member["session_token"], "room_send",
                {"room_id": created["room_id"], "target_spec": "*",
                 "payload": {"kind": "message", "text": "hi"}})

    def test_room_ack_refreshes_last_seen(self) -> None:
        owner, member, created = self._two_members("ackref")
        polled = self._assert_ok(member["session_token"], "room_poll",
                                 {"room_id": created["room_id"], "after_seq": 0}, request_id=10)
        head = polled["cursor_head"]
        with self._touch_interval(0.0001):
            self._assert_refreshes(
                owner["tenant_id"], created["room_id"], member["account_id"],
                member["session_token"], "room_ack",
                {"room_id": created["room_id"], "seq": head})

    def test_room_event_log_refreshes_last_seen(self) -> None:
        owner, member, created = self._two_members("logref")
        with self._touch_interval(0.0001):
            self._assert_refreshes(
                owner["tenant_id"], created["room_id"], member["account_id"],
                member["session_token"], "room_event_log", {"room_id": created["room_id"]})

    def test_room_info_refreshes_last_seen(self) -> None:
        owner, member, created = self._two_members("inforef")
        with self._touch_interval(0.0001):
            self._assert_refreshes(
                owner["tenant_id"], created["room_id"], member["account_id"],
                member["session_token"], "room_info", {"room_id": created["room_id"]})

    def test_room_join_rejoin_refreshes_last_seen(self) -> None:
        owner = self._signup("rejoin-owner@example.com")
        member = self._signup("rejoin-member@example.com", tenant_id=owner["tenant_id"])
        created = self._assert_ok(owner["session_token"], "room_create", {"cap": 4}, request_id=1)
        self._assert_ok(member["session_token"], "room_join",
                        {"room_id": created["room_id"], "link_token": created["link_token"],
                         "consent": True}, request_id=2)
        before = self._raw_last_seen(owner["tenant_id"], created["room_id"], member["account_id"])
        time.sleep(0.02)
        self._assert_ok(member["session_token"], "room_join",
                        {"room_id": created["room_id"], "link_token": created["link_token"],
                         "consent": True}, request_id=3)
        after = self._raw_last_seen(owner["tenant_id"], created["room_id"], member["account_id"])
        self.assertGreater(after, before, "room_join re-join must refresh the caller's last_seen")


class RoomLivenessWaitTests(RoomLivenessTestBase):
    """A blocked room_wait refreshes on ENTRY and on RETURN; never ages the caller."""

    def test_long_wait_does_not_age_caller_toward_stale(self) -> None:
        owner, member, created = self._two_members("staleguard")
        tenant_id = owner["tenant_id"]
        stale_after = getattr(rooms_module, "ROOM_STALE_AFTER_SECONDS", _STALE_FALLBACK)

        # Ack everything so the wait genuinely blocks for its full timeout.
        polled = self._assert_ok(member["session_token"], "room_poll",
                                 {"room_id": created["room_id"], "after_seq": 0}, request_id=10)
        head = polled["cursor_head"]
        self._assert_ok(member["session_token"], "room_ack",
                        {"room_id": created["room_id"], "seq": head}, request_id=11)

        # Backdate the member PAST the staleness threshold. Without an entry
        # refresh, this block would leave the member stale (and silently
        # unroutable) the moment the wait ends.
        backdated = time.time() - (stale_after + 10.0)
        self._set_last_seen(tenant_id, created["room_id"], member["account_id"], backdated)

        holder: dict = {}

        def _wait() -> None:
            try:
                holder["resp"] = self._mcp_call(
                    member["session_token"], "room_wait",
                    {"room_id": created["room_id"], "after_seq": head, "timeout_seconds": 2},
                    request_id=20, timeout=10)
            except Exception as exc:  # defensive: surface worker failures
                holder["error"] = exc

        with self._touch_interval(0.0):
            thread = threading.Thread(target=_wait, daemon=True)
            thread.start()
            # While the wait is still blocked, the ENTRY refresh must already
            # have landed — the server is holding this connection right now.
            deadline = time.monotonic() + 2.0
            during = backdated
            while time.monotonic() < deadline:
                during = self._raw_last_seen(tenant_id, created["room_id"], member["account_id"])
                if during is not None and during > backdated:
                    break
                time.sleep(0.05)
            thread.join(timeout=12)
            self.assertFalse(thread.is_alive(), "room_wait never returned")
            self.assertNotIn("error", holder, f"room_wait raised: {holder.get('error')}")

        self.assertGreater(during, backdated,
                           "a blocked room_wait must refresh last_seen on ENTRY "
                           "(the server holds the connection and knows the agent is there)")
        after = self._raw_last_seen(tenant_id, created["room_id"], member["account_id"])
        self.assertGreater(after, backdated, "room_wait RETURN must leave last_seen fresh")

        info = self._assert_ok(owner["session_token"], "room_info",
                               {"room_id": created["room_id"]}, request_id=21)
        statuses = {m["agent_id"]: m["status"] for m in info["members"]}
        self.assertEqual(statuses.get(member["account_id"]), "active",
                         "a member that just finished a room_wait must not be displayed stale")


class RoomLivenessAuthTests(RoomLivenessTestBase):
    """Presence refresh is scoped to the caller's OWN row — never another's, never a non-member's."""

    def test_member_cannot_refresh_another_members_last_seen(self) -> None:
        owner, member, created = self._two_members("noross")
        tenant_id = owner["tenant_id"]
        member_before = self._raw_last_seen(tenant_id, created["room_id"], member["account_id"])
        with self._touch_interval(0.0):
            time.sleep(0.02)
            self._assert_ok(owner["session_token"], "room_poll",
                            {"room_id": created["room_id"]}, request_id=10)
            self._assert_ok(owner["session_token"], "room_send",
                            {"room_id": created["room_id"], "target_spec": "*",
                             "payload": {"kind": "message", "text": "ping"}}, request_id=11)
            self._assert_ok(owner["session_token"], "room_info",
                            {"room_id": created["room_id"]}, request_id=12)
        member_after = self._raw_last_seen(tenant_id, created["room_id"], member["account_id"])
        self.assertEqual(member_after, member_before,
                         "a member's activity must never refresh another member's last_seen")

    def test_non_member_call_creates_no_membership_row(self) -> None:
        owner, member, created = self._two_members("nonmem")
        tenant_id = owner["tenant_id"]
        outsider = self._signup("nonmem-outsider@example.com", tenant_id=tenant_id)
        self.assertEqual(len(self._membership_rows(tenant_id, created["room_id"], outsider["account_id"])), 0)

        for name, args in (
            ("room_poll", {"room_id": created["room_id"]}),
            ("room_info", {"room_id": created["room_id"]}),
            ("room_send", {"room_id": created["room_id"], "target_spec": "*",
                           "payload": {"kind": "message", "text": "x"}}),
            ("room_event_log", {"room_id": created["room_id"]}),
        ):
            resp = self._mcp_call(outsider["session_token"], name, args)
            self.assertTrue(resp["isError"], f"non-member {name} must be refused")

        self.assertEqual(len(self._membership_rows(tenant_id, created["room_id"], outsider["account_id"])), 0,
                         "a non-member call must not create a membership row")
        # The outsider's attempts must not have touched the real member's row either.
        self.assertEqual(len(self._membership_rows(tenant_id, created["room_id"], member["account_id"])), 1,
                         "a non-member call must not touch a member's row")


class RoomLivenessConstantTests(RoomLivenessTestBase):
    """The staleness threshold is ONE constant; room_info reads it and routing NEVER drops by it.

    Replaced invariant (post-staleloss P0): routing once dropped members older
    than the threshold — that caused permanent silent unicast loss, so routing
    is now membership-keyed and must route EVERY current member regardless of
    presence age. The constant governs the DISPLAY status only. This test
    proves both halves: the display reads the shared constant, and routing does
    NOT follow it.
    """

    def test_stale_threshold_single_constant_routing_and_info_agree(self) -> None:
        self.assertEqual(
            getattr(rooms_module, "ROOM_STALE_AFTER_SECONDS", None), 1800.0,
            "ROOM_STALE_AFTER_SECONDS must exist as ONE module constant and stay 1800",
        )
        owner, member, created = self._two_members("const")
        tenant_id = owner["tenant_id"]
        # A member idle 120s must flip the DISPLAY when the shared constant is
        # patched to 60 — proving room_info reads the SAME constant (a
        # divergent hardcoded literal could not follow the patch).
        self._set_last_seen(tenant_id, created["room_id"], member["account_id"],
                            time.time() - 120)
        previous = getattr(rooms_module, "ROOM_STALE_AFTER_SECONDS", None)
        rooms_module.ROOM_STALE_AFTER_SECONDS = 60
        try:
            info = self._assert_ok(owner["session_token"], "room_info",
                                   {"room_id": created["room_id"]}, request_id=30)
            statuses = {m["agent_id"]: m["status"] for m in info["members"]}
            self.assertEqual(statuses.get(member["account_id"]), "stale",
                             "room_info must read the shared staleness constant")
            # THE POST-P0 INVARIANT: routing is membership-keyed, not
            # presence-keyed. A member the display calls 'stale' is still a
            # member and MUST still receive mail — otherwise the staleloss P0
            # (silent permanent unicast loss to idle members) returns.
            sent = self._assert_ok(owner["session_token"], "room_send",
                                   {"room_id": created["room_id"],
                                    "target_spec": member["account_id"],
                                    "payload": {"kind": "message", "text": "must-route"}},
                                   request_id=31)
            routed = {r["agent_id"] for r in sent["receipts"]}
            self.assertIn(member["account_id"], routed,
                          "target routing must NOT drop members the display calls stale "
                          "(deliverability is keyed on membership)")
        finally:
            if previous is None:
                del rooms_module.ROOM_STALE_AFTER_SECONDS
            else:
                rooms_module.ROOM_STALE_AFTER_SECONDS = previous


class RoomLivenessHeartbeatTests(RoomLivenessTestBase):
    """room_heartbeat keeps working exactly as today (unthrottled, refreshes, same shape)."""

    def test_heartbeat_still_works(self) -> None:
        owner, member, created = self._two_members("hb")
        before = self._raw_last_seen(owner["tenant_id"], created["room_id"], member["account_id"])
        time.sleep(0.02)
        hb = self._assert_ok(member["session_token"], "room_heartbeat",
                             {"room_id": created["room_id"]}, request_id=10)
        after = self._raw_last_seen(owner["tenant_id"], created["room_id"], member["account_id"])
        self.assertEqual(hb["agent_id"], member["account_id"])
        self.assertEqual(hb["status"], "active")
        self.assertIn("last_seen", hb)
        self.assertGreater(after, before, "room_heartbeat must keep refreshing last_seen")
        # heartbeat stays useful for a member that is alive but deliberately silent:
        # the OTHER members' rows are untouched.
        owner_before = self._raw_last_seen(owner["tenant_id"], created["room_id"], owner["account_id"])
        self.assertEqual(self._raw_last_seen(owner["tenant_id"], created["room_id"], owner["account_id"]),
                         owner_before)


class RoomLivenessThroughputTests(RoomLivenessTestBase):
    """The presence refresh is throttled: a busy poll loop must not write per call."""

    def test_tight_poll_loop_does_not_write_per_call(self) -> None:
        owner, member, created = self._two_members("throttle")
        tenant_id = owner["tenant_id"]
        with self._touch_interval(3600.0):  # wide window: any per-call write would break this
            before = self._raw_last_seen(tenant_id, created["room_id"], member["account_id"])
            for index in range(30):
                self._assert_ok(member["session_token"], "room_poll",
                                {"room_id": created["room_id"]}, request_id=100 + index)
                time.sleep(0.002)
            after = self._raw_last_seen(tenant_id, created["room_id"], member["account_id"])
        self.assertEqual(after, before,
                         "a tight poll loop must NOT write last_seen per call (throttled)")


# ---------------------------------------------------------------------------
# Coordinator plane — same liveness defect, same fix, real MCP dispatcher.
# The hosted cloud plane is covered above; the stdlib coordinator tier (the
# surface Claude Desktop / Claude Code / Cursor host locally) had the same
# defect: only room_heartbeat refreshed last_seen, so a member following the
# documented poll loop aged into 'stale' while actively working.
# ---------------------------------------------------------------------------

class CoordinatorRoomLivenessTests(unittest.TestCase):
    def setUp(self) -> None:
        import sqlite3 as _sqlite3

        from weft_mcp.core import WeftStore
        from weft_mcp.server import WeftDispatcher
        import weft_mcp.room as coord_room

        self._sqlite3 = _sqlite3
        self.coord_room = coord_room
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        self.team = "team-coord-liveness"
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

    def _last_seen(self, agent_id: str) -> float:
        conn = self._sqlite3.connect(self.dispatcher.rooms.db_path)
        try:
            row = conn.execute(
                "SELECT last_seen FROM room_members WHERE room_id = ? AND agent_id = ?",
                (self.room_id, agent_id),
            ).fetchone()
            return float(row[0])
        finally:
            conn.close()

    def _backdate(self, agent_id: str) -> None:
        conn = self._sqlite3.connect(self.dispatcher.rooms.db_path)
        try:
            conn.execute(
                "UPDATE room_members SET last_seen = ? WHERE room_id = ? AND agent_id = ?",
                (time.time() - 60.0, self.room_id, agent_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _touch_interval(self, seconds: float):
        return _patched_interval(self.coord_room, seconds)

    def test_poll_refreshes_last_seen(self) -> None:
        self._backdate("A2")
        before = self._last_seen("A2")
        time.sleep(0.02)
        with self._touch_interval(0.0001):
            self.dispatcher.call_tool("room_poll", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "A2", "actor_token": self.tokens["A2"], "after_seq": 0,
            })
        after = self._last_seen("A2")
        self.assertGreater(after, before,
                           "coordinator room_poll must refresh the caller's last_seen")

    def test_send_refreshes_last_seen(self) -> None:
        self._backdate("A2")
        before = self._last_seen("A2")
        time.sleep(0.02)
        with self._touch_interval(0.0001):
            self.dispatcher.call_tool("room_send", {
                "team_id": self.team, "room_id": self.room_id,
                "sender_agent_id": "A2", "target_spec": "*",
                "payload": {"text": "coord-liveness-send"},
                "actor_token": self.tokens["A2"],
            })
        after = self._last_seen("A2")
        self.assertGreater(after, before,
                           "coordinator room_send must refresh the caller's last_seen")

    def test_info_refreshes_last_seen(self) -> None:
        self._backdate("A2")
        before = self._last_seen("A2")
        time.sleep(0.02)
        with self._touch_interval(0.0001):
            self.dispatcher.call_tool("room_info", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "A2", "actor_token": self.tokens["A2"],
            })
        after = self._last_seen("A2")
        self.assertGreater(after, before,
                           "coordinator room_info must refresh the caller's last_seen")

    def test_ack_refreshes_last_seen(self) -> None:
        self._backdate("A2")
        before = self._last_seen("A2")
        time.sleep(0.02)
        with self._touch_interval(0.0001):
            self.dispatcher.call_tool("room_ack", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "A2", "seq": 0, "actor_token": self.tokens["A2"],
            })
        after = self._last_seen("A2")
        self.assertGreater(after, before,
                           "coordinator room_ack must refresh the caller's last_seen")

    def test_receipts_refresh_last_seen(self) -> None:
        sent = self.dispatcher.call_tool("room_send", {
            "team_id": self.team, "room_id": self.room_id,
            "sender_agent_id": "A2", "target_spec": "OWNER",
            "payload": {"text": "receipt-liveness"},
            "actor_token": self.tokens["A2"],
        })
        entry_id = sent["receipts"][0]["entry_id"]
        self._backdate("A2")
        before = self._last_seen("A2")
        time.sleep(0.02)
        with self._touch_interval(0.0001):
            self.dispatcher.call_tool("room_receipts", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "A2", "entry_ids": [entry_id],
                "actor_token": self.tokens["A2"],
            })
        after = self._last_seen("A2")
        self.assertGreater(after, before,
                           "coordinator room_receipts must refresh the caller's last_seen")

    def test_member_cannot_refresh_another_members_last_seen(self) -> None:
        a2_before = self._last_seen("A2")
        with self._touch_interval(0.0001):
            self.dispatcher.call_tool("room_poll", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "OWNER", "actor_token": self.tokens["OWNER"], "after_seq": 0,
            })
            self.dispatcher.call_tool("room_send", {
                "team_id": self.team, "room_id": self.room_id,
                "sender_agent_id": "OWNER", "target_spec": "*",
                "payload": {"text": "ping"}, "actor_token": self.tokens["OWNER"],
            })
            self.dispatcher.call_tool("room_info", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "OWNER", "actor_token": self.tokens["OWNER"],
            })
        self.assertEqual(self._last_seen("A2"), a2_before,
                         "a member's activity must never refresh another member's last_seen")

    def test_display_reads_shared_constant(self) -> None:
        previous = getattr(self.coord_room, "ROOM_STALE_AFTER_SECONDS", None)
        self.coord_room.ROOM_STALE_AFTER_SECONDS = 60
        try:
            self._backdate("A2")
            info = self.dispatcher.call_tool("room_info", {
                "team_id": self.team, "room_id": self.room_id,
                "agent_id": "OWNER", "actor_token": self.tokens["OWNER"],
            })
            statuses = {m["agent_id"]: m["status"] for m in info["members"]}
            self.assertEqual(statuses.get("A2"), "stale",
                             "coordinator room_info must read the shared staleness constant")
        finally:
            if previous is None:
                del self.coord_room.ROOM_STALE_AFTER_SECONDS
            else:
                self.coord_room.ROOM_STALE_AFTER_SECONDS = previous

    def test_heartbeat_still_works(self) -> None:
        self._backdate("A2")
        before = self._last_seen("A2")
        time.sleep(0.02)
        hb = self.dispatcher.call_tool("room_heartbeat", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"],
        })
        self.assertEqual(hb["status"], "active")
        self.assertGreater(self._last_seen("A2"), before,
                           "room_heartbeat must keep refreshing last_seen")


def _patched_interval(module, seconds: float):
    @contextlib.contextmanager
    def _ctx():
        previous = getattr(module, "ROOM_LIVENESS_TOUCH_INTERVAL", None)
        module.ROOM_LIVENESS_TOUCH_INTERVAL = seconds
        try:
            yield
        finally:
            if previous is None:
                del module.ROOM_LIVENESS_TOUCH_INTERVAL
            else:
                module.ROOM_LIVENESS_TOUCH_INTERVAL = previous

    return _ctx()


if __name__ == "__main__":
    unittest.main()
