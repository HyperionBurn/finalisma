"""P0 regression — a unicast to a STALE member is silently lost (RED first).

Measured on LIVE PRODUCTION: a send to a member whose presence is stale
(``last_seen`` older than 1800s) produced ``receipts: []``, no delivery row,
and — after the member returned to active — a poll of the event still returned
``{"redacted": true, "reason": "not_the_addressee"}``. The message is not
delayed; it is permanently lost to its own intended recipient.

``stale`` is a PRESENCE notion (idle), distinct from ``left`` (departed) and
distinct from a revoked credential. The common product case is a peer idling
between turns. The decision encoded by these tests: an idle member is still a
member and must still receive its mail.

Two of these tests are GUARDS, not repros — they must pass BEFORE and AFTER
the fix:
  * the privacy guard (non-addressee still redacted)
  * the active-member regression guard (active behaviour byte-identical)

The other tests are RED repros that must fail against the current code.

Drives the REAL ``WeftCloudService`` over REAL HTTP (never mocks), plus the
coordinator ``RoomStore`` through the real ``WeftDispatcher``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
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
        with urllib.request.urlopen(req, timeout=10) as resp:
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


# STALE_AFTER_SECONDS in the coordinator room model (roster.py:30). The cloud
# plane mirrors it. Anything over this age is "stale".
STALE_AFTER_SECONDS = 1800
STALE_AGE = STALE_AFTER_SECONDS + 500


class CloudStaleDeliveryTestBase(unittest.TestCase):
    """Real HTTP service on a background thread (same seam as test_cloud_service)."""

    def setUp(self) -> None:
        import shutil

        self.tmpdir = tempfile.mkdtemp(prefix="weft-stale-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        from http.server import ThreadingHTTPServer

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.service = WeftCloudService(SqliteWalBackend(self.db_path), origin=self.base)
        _CloudHTTPHandler.service = self.service
        self.server = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self.server.start()

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

    def _signup(self, email: str, password: str) -> dict:
        status, body = _post(self.base, "/v1/auth/signup", {
            "email": email, "password": password,
        })
        self.assertEqual(status, 201, f"signup failed: {body}")
        return body

    def _create_room(self, token: str, cap: int = 6) -> dict:
        status, body = _post(self.base, "/v1/rooms/create", {"cap": cap}, token)
        self.assertEqual(status, 201, f"room create failed: {body}")
        return body

    def _join_room(self, token: str, room_id: str, link_token: str) -> dict:
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room_id, "link_token": link_token, "consent": True,
        }, token)
        self.assertEqual(status, 200, f"join failed: {body}")
        return body

    def _send(self, token: str, room_id: str, target_spec, payload: dict) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/send", {
            "room_id": room_id, "target_spec": target_spec, "payload": payload,
        }, token)

    def _poll(self, token: str, room_id: str, after_seq: int = 0) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/poll", {
            "room_id": room_id, "after_seq": after_seq,
        }, token)

    def _info(self, token: str, room_id: str) -> dict:
        status, body = _get(self.base, f"/v1/rooms/info?room_id={room_id}", token)
        self.assertEqual(status, 200, f"room info failed: {body}")
        return body

    def _force_stale(self, account_id: str, room_id: str) -> None:
        """Back-date last_seen so the member is derived-stale (> STALE_AFTER)."""
        with self.service.backend.transaction() as tx:
            tx.execute(
                "UPDATE cloud_room_members SET last_seen = ? WHERE room_id = ? AND agent_id = ?",
                (time.time() - STALE_AGE, room_id, account_id),
            )

    @staticmethod
    def _message_events(result: dict) -> list[dict]:
        return [e for e in result["events"] if e["kind"] == "room.message"]


class TestUnicastToStaleMemberDelivered(CloudStaleDeliveryTestBase):
    """THE repro: a unicast to a stale member must reach them on return."""

    def test_stale_member_reads_the_message_after_returning_active(self) -> None:
        owner = self._signup("stale-owner@example.com", "OwnerPass!1")
        room = self._create_room(owner["session_token"], cap=4)
        peer = self._signup("stale-peer@example.com", "PeerPass!1")
        self._join_room(peer["session_token"], room["room_id"], room["link_token"])

        # Confirm the peer is currently ACTIVE.
        info = self._info(owner["session_token"], room["room_id"])
        status_by_id = {m["agent_id"]: m["status"] for m in info["members"]}
        self.assertEqual(status_by_id[peer["account_id"]], "active")

        # Let the peer idle past the stale threshold.
        self._force_stale(peer["account_id"], room["room_id"])
        info = self._info(owner["session_token"], room["room_id"])
        status_by_id = {m["agent_id"]: m["status"] for m in info["members"]}
        self.assertEqual(status_by_id[peer["account_id"]], "stale",
                         "setup: peer must be derived-stale")

        # Unicast to the stale member. This MUST produce a durable receipt —
        # RED before the fix: receipts were [] because stale members were
        # excluded from routing, so no delivery row was ever created.
        status, sent = self._send(
            owner["session_token"], room["room_id"], peer["account_id"],
            {"text": "SOS-STALE-UNICAST"},
        )
        self.assertEqual(status, 200, f"send to stale member failed: {sent}")
        receipt_agents = [r["agent_id"] for r in sent["receipts"]]
        self.assertEqual(receipt_agents, [peer["account_id"]],
                         "a stale member is still a member and must receive a "
                         "durable delivery receipt")

        # The peer returns to active and re-polls. The message addressed to it
        # must be readable — RED before the fix: no delivery row existed, so
        # the redaction check did not count the peer as an addressee and the
        # poll stayed redacted forever.
        status, hb = _post(self.base, "/v1/rooms/heartbeat", {
            "room_id": room["room_id"],
        }, peer["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(hb["status"], "active")
        info = self._info(owner["session_token"], room["room_id"])
        status_by_id = {m["agent_id"]: m["status"] for m in info["members"]}
        self.assertEqual(status_by_id[peer["account_id"]], "active")

        status, polled = self._poll(peer["session_token"], room["room_id"], after_seq=0)
        self.assertEqual(status, 200)
        msgs = self._message_events(polled)
        readable = [e for e in msgs
                    if isinstance(e["payload"], dict)
                    and isinstance(e["payload"].get("payload"), dict)
                    and e["payload"]["payload"].get("text") == "SOS-STALE-UNICAST"]
        self.assertEqual(len(readable), 1,
                         "the stale member must be able to read the message "
                         "addressed to it once it returns to active")

    def test_non_addressee_still_redacted_while_target_is_stale(self) -> None:
        """Privacy guard (PASSES before the fix): widening who receives must
        NOT widen who can read."""
        owner = self._signup("priv-owner@example.com", "OwnerPass!1")
        room = self._create_room(owner["session_token"], cap=5)
        peer = self._signup("priv-peer@example.com", "PeerPass!1")
        outsider = self._signup("priv-outsider@example.com", "OutPass!1")
        self._join_room(peer["session_token"], room["room_id"], room["link_token"])
        self._join_room(outsider["session_token"], room["room_id"], room["link_token"])
        self._force_stale(peer["account_id"], room["room_id"])

        status, sent = self._send(
            owner["session_token"], room["room_id"], peer["account_id"],
            {"text": "TOP-SECRET-STALE"},
        )
        self.assertEqual(status, 200)

        status, polled = self._poll(outsider["session_token"], room["room_id"], after_seq=0)
        self.assertEqual(status, 200)
        msg = self._message_events(polled)[-1]
        self.assertEqual(msg["payload"],
                         {"redacted": True, "reason": "not_the_addressee"})
        self.assertNotIn("TOP-SECRET-STALE", json.dumps(polled),
                         "a non-addressee must never see the unicast body")


class TestBroadcastToStalePeers(CloudStaleDeliveryTestBase):
    """Broadcast ``*`` with every peer stale must still deliver (same root cause)."""

    def test_broadcast_reaches_stale_peers_and_they_can_read_on_return(self) -> None:
        owner = self._signup("bc-owner@example.com", "OwnerPass!1")
        room = self._create_room(owner["session_token"], cap=5)
        peers = []
        for i in range(2):
            s = self._signup(f"bc-peer{i}@example.com", f"PeerPass-{i}!1")
            self._join_room(s["session_token"], room["room_id"], room["link_token"])
            peers.append(s)

        for peer in peers:
            self._force_stale(peer["account_id"], room["room_id"])

        status, sent = self._send(
            owner["session_token"], room["room_id"], "*", {"text": "BC-TO-EVERYONE"},
        )
        self.assertEqual(status, 200)
        receipt_agents = sorted(r["agent_id"] for r in sent["receipts"])
        # RED before the fix: '*' excluded every stale peer, so receipts were [].
        self.assertEqual(receipt_agents,
                         sorted(peer["account_id"] for peer in peers),
                         "broadcast must deliver to every member, stale or not")

        # Each peer returns to active and can read the broadcast.
        for peer in peers:
            status, hb = _post(self.base, "/v1/rooms/heartbeat", {
                "room_id": room["room_id"],
            }, peer["session_token"])
            self.assertEqual(status, 200)
            self.assertEqual(hb["status"], "active")
            status, polled = self._poll(peer["session_token"], room["room_id"], after_seq=0)
            self.assertEqual(status, 200)
            msgs = self._message_events(polled)
            self.assertEqual(len(msgs), 1, "each peer must see the broadcast event")
            self.assertEqual(msgs[-1]["payload"]["payload"]["text"], "BC-TO-EVERYONE")


class TestSendToNonexistentTargetDistinguishable(CloudStaleDeliveryTestBase):
    """NO SILENT SUCCESS: an undeliverable named target must never look like success."""

    def test_unknown_recipient_refused_not_silent_success(self) -> None:
        owner = self._signup("ghost-owner@example.com", "OwnerPass!1")
        room = self._create_room(owner["session_token"], cap=4)
        peer = self._signup("ghost-peer@example.com", "PeerPass!1")
        self._join_room(peer["session_token"], room["room_id"], room["link_token"])

        # A unicast to an agent that never joined: RED before the fix — the
        # send returned 200 with receipts: [], indistinguishable from a
        # broadcast to zero members or a typo'd list.
        status, body = self._send(
            owner["session_token"], room["room_id"], "ghost-agent-xyz",
            {"text": "hi-ghost"},
        )
        self.assertEqual(status, 422,
                         "a send to a target that does not exist must be refused")
        self.assertEqual(body["error"]["code"], "recipient_not_found")

        # A successful send to a real member stays 200 with a receipt — the
        # two cases are now distinguishable from the response alone.
        status, ok = self._send(
            owner["session_token"], room["room_id"], peer["account_id"],
            {"text": "hi-real"},
        )
        self.assertEqual(status, 200)
        self.assertEqual([r["agent_id"] for r in ok["receipts"]],
                         [peer["account_id"]])

    def test_unicast_to_left_member_refused(self) -> None:
        """Departure (``left``) is genuinely undeliverable — refused, not silent."""
        owner = self._signup("left-owner@example.com", "OwnerPass!1")
        room = self._create_room(owner["session_token"], cap=4)
        peer = self._signup("left-peer@example.com", "PeerPass!1")
        self._join_room(peer["session_token"], room["room_id"], room["link_token"])

        status, left = _post(self.base, "/v1/rooms/leave", {
            "room_id": room["room_id"],
        }, peer["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(left["status"], "left")

        status, body = self._send(
            owner["session_token"], room["room_id"], peer["account_id"],
            {"text": "you-left"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "recipient_not_found")


class TestActiveMemberUnchanged(CloudStaleDeliveryTestBase):
    """Regression guard: behaviour for an ACTIVE member is byte-identical."""

    def test_unicast_to_active_member_unchanged(self) -> None:
        owner = self._signup("act-owner@example.com", "OwnerPass!1")
        room = self._create_room(owner["session_token"], cap=4)
        peer = self._signup("act-peer@example.com", "PeerPass!1")
        self._join_room(peer["session_token"], room["room_id"], room["link_token"])

        status, sent = self._send(
            owner["session_token"], room["room_id"], peer["account_id"],
            {"text": "hi-active"},
        )
        self.assertEqual(status, 200)
        self.assertEqual([r["agent_id"] for r in sent["receipts"]],
                         [peer["account_id"]])
        self.assertIn("seq", sent)

        status, polled = self._poll(peer["session_token"], room["room_id"], after_seq=0)
        self.assertEqual(status, 200)
        msgs = self._message_events(polled)
        self.assertEqual(msgs[-1]["payload"]["payload"]["text"], "hi-active")


class TestRejoinIdempotent(CloudStaleDeliveryTestBase):
    """Rejoining a room you are already in must not silently mutate membership."""

    def test_rejoin_does_not_mutate_joined_at(self) -> None:
        owner = self._signup("rj-owner@example.com", "OwnerPass!1")
        room = self._create_room(owner["session_token"], cap=4)
        peer = self._signup("rj-peer@example.com", "PeerPass!1")
        r1 = self._join_room(peer["session_token"], room["room_id"], room["link_token"])
        r2 = self._join_room(peer["session_token"], room["room_id"], room["link_token"])

        # RED before the fix: the active-rejoin path overwrote joined_at
        # (07:16:01 -> 07:53:18) with no new membership row.
        self.assertEqual(r2["joined_at"], r1["joined_at"],
                         "rejoin of an already-active member must not rewrite joined_at")
        info = self._info(owner["session_token"], room["room_id"])
        self.assertEqual(info["member_count"], 2,
                         "idempotent rejoin must not add a second seat")

    def test_join_response_reports_real_cursor(self) -> None:
        owner = self._signup("cur-owner@example.com", "OwnerPass!1")
        room = self._create_room(owner["session_token"], cap=4)
        peer = self._signup("cur-peer@example.com", "PeerPass!1")
        self._join_room(peer["session_token"], room["room_id"], room["link_token"])

        # Advance the peer's durable cursor.
        status, sent = self._send(
            owner["session_token"], room["room_id"], "*", {"text": "advance"},
        )
        self.assertEqual(status, 200)
        status, polled = self._poll(peer["session_token"], room["room_id"], after_seq=0)
        self.assertEqual(status, 200)
        head = polled["cursor_head"]
        self.assertGreater(head, 0, "setup: room must have a non-zero cursor head")
        status, acked = _post(self.base, "/v1/rooms/ack", {
            "room_id": room["room_id"], "seq": head,
        }, peer["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(acked["last_ack_seq"], head)

        # Rejoin must report the member's ACTUAL cursor — RED before the fix:
        # the join response always returned cursor: 0, a false state report.
        rejoined = self._join_room(peer["session_token"], room["room_id"], room["link_token"])
        self.assertEqual(rejoined["cursor"], head,
                         "join must report the caller's real cursor, not 0")


# ---------------------------------------------------------------------------
# Coordinator plane — same four defects, same fixes, real MCP dispatcher.
# ---------------------------------------------------------------------------

class CoordinatorRoomStaleDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        from weft_mcp.core import WeftStore
        from weft_mcp.server import WeftDispatcher

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        self.team = "team-stale"
        self.tokens = {}
        for agent_id in ("OWNER", "A2", "A3"):
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
        for agent_id in ("OWNER", "A2", "A3"):
            self.dispatcher.call_tool("room_join", {
                "team_id": self.team, "room_id": self.room_id,
                "link_token": self.link_token, "agent_id": agent_id,
                "consent": True, "actor_token": self.tokens[agent_id],
            })

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _backdate(self, agent_id: str) -> None:
        conn = sqlite3.connect(self.dispatcher.rooms.db_path)
        try:
            conn.execute(
                "UPDATE room_members SET last_seen = ? WHERE room_id = ? AND agent_id = ?",
                (time.time() - STALE_AGE, self.room_id, agent_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _send(self, sender: str, target_spec, payload: dict) -> dict:
        return self.dispatcher.call_tool("room_send", {
            "team_id": self.team, "room_id": self.room_id,
            "sender_agent_id": sender, "target_spec": target_spec,
            "payload": payload, "actor_token": self.tokens[sender],
        })

    def _poll(self, agent: str, after_seq: int = 0) -> dict:
        return self.dispatcher.call_tool("room_poll", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": agent, "actor_token": self.tokens[agent],
            "after_seq": after_seq,
        })

    def test_stale_unicast_receives_receipt_and_is_readable_after_return(self) -> None:
        self._backdate("A2")
        sent = self._send("OWNER", "A2", {"text": "coordinator-stale-unicast"})
        receipt_agents = [r["agent_id"] for r in sent["receipts"]]
        self.assertEqual(receipt_agents, ["A2"],
                         "coordinator: stale member must receive a delivery receipt")

        hb = self.dispatcher.call_tool("room_heartbeat", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "actor_token": self.tokens["A2"],
        })
        self.assertEqual(hb["status"], "active")

        polled = self._poll("A2")
        msgs = [e for e in polled["events"] if e["kind"] == "room.message"]
        self.assertGreaterEqual(len(msgs), 1)
        self.assertEqual(msgs[-1]["payload"]["payload"]["text"],
                         "coordinator-stale-unicast")

    def test_coordinator_unicast_is_redacted_for_non_addressee(self) -> None:
        sent = self._send("OWNER", "A2", {"text": "coordinator-private"})
        a2_events = [e for e in self._poll("A2")["events"] if e["seq"] == sent["seq"]]
        a3_events = [e for e in self._poll("A3")["events"] if e["seq"] == sent["seq"]]
        self.assertEqual(a2_events[0]["payload"]["payload"]["text"], "coordinator-private")
        self.assertEqual(
            a3_events[0]["payload"],
            {"redacted": True, "reason": "not_the_addressee"},
            "coordinator room_poll must not expose a private unicast to another member",
        )

    def test_coordinator_sender_can_audit_unicast_and_receipt_read_status(self) -> None:
        sent = self._send("OWNER", "A2", {"text": "coordinator-audit"})
        self.assertEqual(sent["receipts"][0]["read_status"], "queued")
        owner_events = [e for e in self._poll("OWNER")["events"] if e["seq"] == sent["seq"]]
        self.assertEqual(owner_events[0]["payload"]["payload"]["text"], "coordinator-audit")

        entry_id = sent["receipts"][0]["entry_id"]
        before = self.dispatcher.call_tool("room_receipts", {
            "team_id": self.team,
            "room_id": self.room_id,
            "agent_id": "OWNER",
            "entry_ids": [entry_id],
            "actor_token": self.tokens["OWNER"],
        })
        self.assertEqual(before["receipts"][0]["read_status"], "queued")

        self._poll("A2")
        acked = self.dispatcher.call_tool("room_ack", {
            "team_id": self.team,
            "room_id": self.room_id,
            "agent_id": "A2",
            "seq": sent["seq"],
            "actor_token": self.tokens["A2"],
        })
        self.assertEqual(acked["receipts_read"], 1)
        after = self.dispatcher.call_tool("room_receipts", {
            "team_id": self.team,
            "room_id": self.room_id,
            "agent_id": "OWNER",
            "entry_ids": [entry_id],
            "actor_token": self.tokens["OWNER"],
        })
        self.assertEqual(after["receipts"][0]["read_status"], "read")

    def test_coordinator_receipts_are_sender_scoped(self) -> None:
        sent = self._send("A2", "OWNER", {"text": "sender-private-receipt"})
        entry_id = sent["receipts"][0]["entry_id"]

        owner_view = self.dispatcher.call_tool("room_receipts", {
            "team_id": self.team,
            "room_id": self.room_id,
            "agent_id": "OWNER",
            "entry_ids": [entry_id],
            "actor_token": self.tokens["OWNER"],
        })
        self.assertEqual(owner_view["receipts"], [{
            "entry_id": entry_id,
            "status": "not_found",
            "read_status": "unknown",
            "attempts": 0,
            "next_attempt_at": None,
            "last_error": None,
        }])

        sender_view = self.dispatcher.call_tool("room_receipts", {
            "team_id": self.team,
            "room_id": self.room_id,
            "agent_id": "A2",
            "entry_ids": [entry_id],
            "actor_token": self.tokens["A2"],
        })
        self.assertEqual(sender_view["receipts"][0]["status"], "queued")
        self.assertEqual(sender_view["receipts"][0]["read_status"], "queued")

    def test_coordinator_receipts_validate_bounded_entry_ids(self) -> None:
        from weft_mcp.core import WeftError

        base = {
            "team_id": self.team,
            "room_id": self.room_id,
            "agent_id": "OWNER",
            "actor_token": self.tokens["OWNER"],
        }
        for entry_ids in ("not-a-list", [""], ["ok", 7], ["ok"] * 201):
            with self.subTest(entry_ids=entry_ids), self.assertRaises(WeftError) as ctx:
                self.dispatcher.call_tool("room_receipts", {
                    **base,
                    "entry_ids": entry_ids,
                })
            self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_coordinator_broadcast_remains_visible_to_all_members(self) -> None:
        sent = self._send("OWNER", "*", {"text": "coordinator-public"})
        for agent_id in ("A2", "A3"):
            events = [e for e in self._poll(agent_id)["events"] if e["seq"] == sent["seq"]]
            self.assertEqual(events[0]["payload"]["payload"]["text"], "coordinator-public")

    def test_legacy_targeted_event_fails_closed(self) -> None:
        # Simulate a pre-redaction event written by an older coordinator. The
        # recipient list is absent, so replay must not leak its body to anyone.
        with self.store._transaction() as conn:
            seq = int(conn.execute(
                "SELECT cursor_head FROM room_rooms WHERE room_id = ?",
                (self.room_id,),
            ).fetchone()[0]) + 1
            conn.execute(
                "INSERT INTO room_event_log(event_id, room_id, seq, origin_agent, kind, "
                "message_kind, payload_json, idempotency_key, trace_id, created_at) "
                "VALUES (?, ?, ?, ?, 'room.message', NULL, ?, ?, NULL, ?)",
                (
                    f"legacy-{seq}", self.room_id, seq, "OWNER",
                    json.dumps({"payload": {"text": "legacy-secret"}, "target_spec": "A2"}),
                    f"legacy-idem-{seq}", "2026-08-13T00:00:00Z",
                ),
            )
            conn.execute(
                "UPDATE room_rooms SET cursor_head = ? WHERE room_id = ?",
                (seq, self.room_id),
            )
        for agent_id in ("A2", "A3"):
            events = [e for e in self._poll(agent_id)["events"] if e["seq"] == seq]
            self.assertEqual(events[0]["payload"], {"redacted": True, "reason": "not_the_addressee"})

    def test_unknown_unicast_target_refused(self) -> None:
        from weft_mcp.core import WeftError
        with self.assertRaises(WeftError) as ctx:
            self._send("OWNER", "ghost-coord", {"text": "hi"})
        self.assertEqual(ctx.exception.code, "recipient_not_found")

    def test_rejoin_preserves_joined_at_and_reports_cursor(self) -> None:
        r1 = self.dispatcher.call_tool("room_join", {
            "team_id": self.team, "room_id": self.room_id,
            "link_token": self.link_token, "agent_id": "A2",
            "consent": True, "actor_token": self.tokens["A2"],
        })
        # Advance A2's cursor.
        self._send("OWNER", "A2", {"text": "advance"})
        polled = self._poll("A2")
        head = polled["cursor_head"]
        self.assertGreater(head, 0)
        self.dispatcher.call_tool("room_ack", {
            "team_id": self.team, "room_id": self.room_id,
            "agent_id": "A2", "seq": head, "actor_token": self.tokens["A2"],
        })
        r2 = self.dispatcher.call_tool("room_join", {
            "team_id": self.team, "room_id": self.room_id,
            "link_token": self.link_token, "agent_id": "A2",
            "consent": True, "actor_token": self.tokens["A2"],
        })
        self.assertEqual(r2["joined_at"], r1["joined_at"],
                         "coordinator: rejoin must not rewrite joined_at")
        self.assertEqual(r2["cursor"], head,
                         "coordinator: join must report the real cursor, not 0")


if __name__ == "__main__":
    unittest.main()
