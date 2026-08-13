"""BUG — revoking an agent key must release its room seats (production repro).

Measured on live production (build a727a3b):

    room_info                        -> 3 members / cap 3
    revoke the agent key             -> 200, credential dead
    room_info again                  -> STILL 3/3, revoked key still listed
    a brand-new agent tries to join  -> 409 room_full

Each agent key is a distinct room member (per-key identity, correct). Revoking
a key kills the credential but used to leave the membership row counting toward
the cap — and that row is then UNREACHABLE, because the credential that owned it
can no longer authenticate to call ``leave``. Rotating a key permanently burned
a seat.

The mechanism already exists: ``cloud_room_members.status`` ('active'/'stale'/
'left'), the cap check that counts only active members, and rejoin restoring a
row. The fix marks the revoked key's memberships ``left`` IN THE SAME transaction
as the revoke, so the seat frees while the row and the event log keep full
attribution. This suite:

  - fills a room to cap, revokes one key, and shows a NEW agent can then join
    (the exact production repro)
  - proves events authored by the revoked identity stay readable + attributed
  - proves room_info member_count drops and the member is no longer active
  - proves org offboarding (remove_member) releases seats too
  - proves releasing one key's seat never disturbs another key's membership
  - proves the revoked key itself still cannot authenticate

Every HTTP test drives the real wire surface (real server, real SQLite), never
an internal function.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.identity import accounts, agent_keys, orgs, sessions
from weft_cloud.identity.context import SessionContext
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.identity.tokens import AuthError
from weft_cloud.rooms import CloudRoomService
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


def _get(base: str, path: str, token: str | None = None, query: str = "") -> tuple[int, dict]:
    req = urllib.request.Request(base + path + query, method="GET")
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


class RoomSeatReleaseBase(unittest.TestCase):
    """Real cloud HTTP service on a fresh server + SQLite per test."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="weft-seat-release-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.service = WeftCloudService(SqliteWalBackend(self.db_path))
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

    # -- wire helpers ------------------------------------------------------

    def _signup(self, email: str) -> dict:
        status, resp = _post(self.base, "/v1/auth/signup",
                             {"email": email, "password": "password-123"})
        self.assertEqual(status, 201, f"signup failed: {resp}")
        return resp

    def _mint_key(self, session: str, label: str = "default") -> dict:
        status, resp = _post(self.base, "/v1/agent-keys", {"label": label}, token=session)
        self.assertEqual(status, 201, f"create key failed: {resp}")
        return resp

    def _create_room(self, token: str, name: str = "room", cap: int = 10) -> dict:
        status, resp = _post(self.base, "/v1/rooms/create", {"name": name, "cap": cap}, token=token)
        self.assertEqual(status, 201, f"create room failed: {resp}")
        return resp

    def _join_room(self, token: str, room_id: str, link_token: str) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/join",
                     {"room_id": room_id, "link_token": link_token, "consent": True},
                     token=token)

    def _room_info(self, token: str, room_id: str) -> tuple[int, dict]:
        return _get(self.base, "/v1/rooms/info", token=token, query=f"?room_id={room_id}")

    def _send(self, token: str, room_id: str, payload: dict, target_spec: str | list = "*") -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/send",
                     {"room_id": room_id, "target_spec": target_spec, "payload": payload},
                     token=token)

    def _poll(self, token: str, room_id: str, after_seq: int = 0) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/poll",
                     {"room_id": room_id, "after_seq": after_seq}, token=token)

    def _event_log(self, token: str, room_id: str) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/event_log", {"room_id": room_id}, token=token)

    def _revoke_key(self, session: str, key_id: str) -> dict:
        status, resp = _post(self.base, "/v1/agent-keys/revoke", {"key_id": key_id}, token=session)
        self.assertEqual(status, 200, f"revoke failed: {resp}")
        self.assertIs(resp["revoked"], True)
        return resp

    def _three_member_room(self, email: str, cap: int = 3) -> tuple[dict, dict, dict, dict, dict]:
        """Signup + two keys; session-owner room of ``cap``; both keys join -> full.

        Returns (acct, session, key1, key2, room) with 3 active identities
        (the account session + two key identities).
        """
        acct = self._signup(email)
        session = acct["session_token"]
        key1 = self._mint_key(session, "k1")
        key2 = self._mint_key(session, "k2")
        room = self._create_room(session, name="seat", cap=cap)
        for key in (key1, key2):
            status, resp = self._join_room(key["agent_key"], room["room_id"], room["link_token"])
            self.assertEqual(status, 200, f"join failed: {resp}")
        return acct, session, key1, key2, room


class RoomSeatReleaseTests(RoomSeatReleaseBase):
    """Revoking an agent key releases its room seats."""

    def test_revoke_frees_a_seat_so_a_new_agent_can_join(self) -> None:
        """THE production repro: full room -> revoke -> a new agent joins."""
        acct, session, key1, key2, room = self._three_member_room("repro@example.com", cap=3)

        # Room is full: a brand-new identity is refused room_full.
        key3 = self._mint_key(session, "k3")
        status, refused = self._join_room(key3["agent_key"], room["room_id"], room["link_token"])
        self.assertEqual(status, 409, f"room must be full before the revoke: {refused}")
        self.assertEqual(refused["error"]["code"], "room_full")

        status, info = self._room_info(session, room["room_id"])
        self.assertEqual(status, 200)
        self.assertEqual(info["member_count"], 3)
        self.assertEqual(info["cap"], 3)

        # Revoke key1: 200 and the credential is dead, but the seat must free.
        self._revoke_key(session, key1["key_id"])
        status, resp = self._poll(key1["agent_key"], room["room_id"])
        self.assertEqual(status, 401, "the revoked key itself must be refused")

        # The SAME new key that was refused moments ago can now join.
        status, joined = self._join_room(key3["agent_key"], room["room_id"], room["link_token"])
        self.assertEqual(status, 200, f"new agent must join after the revoke: {joined}")
        self.assertEqual(joined["agent_id"], key3["key_id"])

    def test_room_info_member_count_drops_and_released_member_not_active(self) -> None:
        acct, session, key1, key2, room = self._three_member_room("droproster@example.com", cap=3)
        self._revoke_key(session, key1["key_id"])

        status, info = self._room_info(session, room["room_id"])
        self.assertEqual(status, 200)
        self.assertEqual(info["member_count"], 2,
                         "member_count must drop when a key is revoked")
        member_ids = {m["agent_id"] for m in info["members"]}
        self.assertNotIn(key1["key_id"], member_ids,
                         "the released member must not be listed as active")
        self.assertIn(key2["key_id"], member_ids,
                      "a surviving key must still be listed as active")

    def test_events_by_revoked_identity_stay_readable_and_attributed(self) -> None:
        acct, session, key1, key2, room = self._three_member_room("history@example.com", cap=3)
        # key1 authors events before being revoked.
        status, sent = self._send(key1["agent_key"], room["room_id"], {"text": "authored-by-key1"})
        self.assertEqual(status, 200, f"send failed: {sent}")

        self._revoke_key(session, key1["key_id"])

        # A surviving member still reads key1's past events, attributed to key1.
        status, events = self._event_log(key2["agent_key"], room["room_id"])
        self.assertEqual(status, 200)
        messages = [e for e in events["events"] if e["kind"] == "room.message"]
        key1_msgs = [e for e in messages if e["origin_agent"] == key1["key_id"]]
        self.assertTrue(key1_msgs, "events authored by the revoked key must survive")
        self.assertEqual(key1_msgs[-1]["payload"]["payload"]["text"], "authored-by-key1")
        for e in key1_msgs:
            self.assertEqual(e["origin_agent"], key1["key_id"],
                             "past events must keep their author attribution")

        # The membership row is preserved (status='left'), never deleted.
        with self.service.backend.transaction() as tx:
            row = tx.execute(
                "SELECT status FROM cloud_room_members "
                "WHERE tenant_id = (SELECT tenant_id FROM cloud_rooms WHERE room_id = ?) "
                "AND room_id = ? AND agent_id = ?",
                (room["room_id"], room["room_id"], key1["key_id"]),
            ).fetchone()
        self.assertIsNotNone(row, "the membership row must not be deleted")
        self.assertEqual(row["status"], "left",
                         "a revoked key's membership is marked left, not deleted")

    def test_releasing_one_keys_seat_does_not_disturb_another_key(self) -> None:
        acct, session, key1, key2, room = self._three_member_room("sibling@example.com", cap=3)
        self._revoke_key(session, key1["key_id"])

        # key2 keeps its seat and its full capability.
        status, info = self._room_info(key2["agent_key"], room["room_id"])
        self.assertEqual(status, 200)
        self.assertEqual(info["member_count"], 2)
        self.assertIn(key2["key_id"], {m["agent_id"] for m in info["members"]})

        status, sent = self._send(key2["agent_key"], room["room_id"], {"text": "still-alive"})
        self.assertEqual(status, 200, f"surviving key must still send: {sent}")
        status, poll = self._poll(key2["agent_key"], room["room_id"], after_seq=0)
        self.assertEqual(status, 200)
        self.assertIn("still-alive", json.dumps(poll["events"]))

    def test_revoked_key_still_cannot_authenticate(self) -> None:
        acct, session, key1, key2, room = self._three_member_room("noregress@example.com", cap=3)
        self._revoke_key(session, key1["key_id"])

        # The revoked credential is refused on the very next request.
        status, resp = self._poll(key1["agent_key"], room["room_id"])
        self.assertEqual(status, 401)
        self.assertEqual(resp["error"]["code"], "invalid_session")
        status, resp = _get(self.base, "/v1/me", token=key1["agent_key"])
        self.assertEqual(status, 401)
        self.assertEqual(resp["error"]["code"], "invalid_session")


class CrossTenantSeatReleaseTests(RoomSeatReleaseBase):
    """Seat release must work when the released identity joined ANOTHER tenant's room.

    Cross-tenant joining is the product's headline feature: an agent that signs
    up under its own brand-new org redeems someone else's link, and the link IS
    the authorization. The membership row is therefore stored under the ROOM's
    tenant, NOT the joiner's — see ``CloudRoomService.join_room`` (``real_tenant_id``).

    A release path that matches on ``(tenant_id, agent_id)`` (where tenant_id is
    the KEY's owning tenant) therefore matches ZERO rows for a cross-tenant
    join, so revoking a key that had joined someone else's room permanently
    burned the seat: ``409 room_full`` forever, with the membership row
    unreachable. This is the exact live-production repro from the suite
    docstring, with the joiner in a different tenant. These tests drive the
    real wire surface (real server, real SQLite).
    """

    def _cross_tenant_room(self, cap: int = 3) -> dict:
        """Owner org in tenant X creates a room; two keys from tenant Y join it.

        Returns the owner session, the room, the outsider session, and the two
        outsider keys. The key identities live in tenant Y (their owner org);
        their membership rows live under the ROOM's tenant X.
        """
        owner = self._signup("owner@xtenant.example.com")
        x_session = owner["session_token"]
        x_tenant = owner["tenant_id"]
        room = self._create_room(x_session, name="cross", cap=cap)

        outsider = self._signup("outsider@ytenant.example.com")
        y_session = outsider["session_token"]
        y_tenant = outsider["tenant_id"]
        self.assertNotEqual(x_tenant, y_tenant,
                            "the two signups must land in different orgs")

        key_a = self._mint_key(y_session, "crossA")
        key_b = self._mint_key(y_session, "crossB")
        for key in (key_a, key_b):
            status, resp = self._join_room(key["agent_key"], room["room_id"], room["link_token"])
            self.assertEqual(status, 200, f"cross-tenant join failed: {resp}")
        return {
            "x_session": x_session, "x_tenant": x_tenant,
            "room": room, "y_session": y_session, "y_tenant": y_tenant,
            "key_a": key_a, "key_b": key_b,
        }

    def _members_counter(self, room_id: str) -> int:
        with self.service.backend.transaction() as tx:
            row = tx.execute(
                "SELECT value FROM cloud_room_counters "
                "WHERE room_id = ? AND counter = 'members'",
                (room_id,),
            ).fetchone()
        return int(row["value"]) if row else 0

    def test_cross_tenant_revoke_frees_seat_so_new_agent_can_join(self) -> None:
        """THE production repro, cross-tenant: full room -> revoke -> new agent joins."""
        s = self._cross_tenant_room(cap=3)

        # Room is full: a brand-new identity is refused room_full.
        key_c = self._mint_key(s["y_session"], "crossC")
        status, refused = self._join_room(key_c["agent_key"], s["room"]["room_id"], s["room"]["link_token"])
        self.assertEqual(status, 409, f"room must be full before the revoke: {refused}")
        self.assertEqual(refused["error"]["code"], "room_full")

        status, info = self._room_info(s["x_session"], s["room"]["room_id"])
        self.assertEqual(status, 200)
        self.assertEqual(info["member_count"], 3)
        self.assertEqual(info["cap"], 3)
        self.assertEqual(self._members_counter(s["room"]["room_id"]), 3)

        # Revoke key_a: it belongs to tenant Y, but its seat lives in tenant X's room.
        self._revoke_key(s["y_session"], s["key_a"]["key_id"])

        # The per-room member counter must drop with the active set.
        self.assertEqual(self._members_counter(s["room"]["room_id"]), 2,
                         "the cross-tenant member counter must decrement on revoke")

        # The SAME key that was refused moments ago can now join.
        status, joined = self._join_room(key_c["agent_key"], s["room"]["room_id"], s["room"]["link_token"])
        self.assertEqual(status, 200, f"cross-tenant seat must free after revoke: {joined}")
        self.assertEqual(joined["agent_id"], key_c["key_id"])
        self.assertEqual(self._members_counter(s["room"]["room_id"]), 3)

    def test_cross_tenant_revoke_drops_count_and_leaves_sibling_untouched(self) -> None:
        s = self._cross_tenant_room(cap=3)
        self._revoke_key(s["y_session"], s["key_a"]["key_id"])

        status, info = self._room_info(s["x_session"], s["room"]["room_id"])
        self.assertEqual(status, 200)
        self.assertEqual(info["member_count"], 2,
                         "cross-tenant member_count must drop when a key is revoked")
        ids = {m["agent_id"] for m in info["members"]}
        self.assertNotIn(s["key_a"]["key_id"], ids,
                         "the released cross-tenant key must not be listed as active")
        self.assertIn(s["key_b"]["key_id"], ids,
                      "a surviving sibling key must still be listed as active")

        # key_b keeps its seat AND its full capability in the room.
        status, sent = self._send(s["key_b"]["agent_key"], s["room"]["room_id"], {"text": "sibling-alive"})
        self.assertEqual(status, 200, f"surviving sibling key must still send: {sent}")
        status, poll = self._poll(s["key_b"]["agent_key"], s["room"]["room_id"], after_seq=0)
        self.assertEqual(status, 200)
        self.assertIn("sibling-alive", json.dumps(poll["events"]))

    def test_cross_tenant_events_by_revoked_identity_stay_readable_and_attributed(self) -> None:
        s = self._cross_tenant_room(cap=3)
        # key_a authors events before being revoked.
        status, sent = self._send(s["key_a"]["agent_key"], s["room"]["room_id"],
                                  {"text": "authored-across-tenant"})
        self.assertEqual(status, 200, f"send failed: {sent}")

        self._revoke_key(s["y_session"], s["key_a"]["key_id"])

        # A surviving member still reads key_a's past events, attributed to key_a.
        status, events = self._event_log(s["key_b"]["agent_key"], s["room"]["room_id"])
        self.assertEqual(status, 200)
        messages = [e for e in events["events"] if e["kind"] == "room.message"]
        key_a_msgs = [e for e in messages if e["origin_agent"] == s["key_a"]["key_id"]]
        self.assertTrue(key_a_msgs, "cross-tenant events authored by the revoked key must survive")
        self.assertEqual(key_a_msgs[-1]["payload"]["payload"]["text"], "authored-across-tenant")
        for e in key_a_msgs:
            self.assertEqual(e["origin_agent"], s["key_a"]["key_id"],
                             "cross-tenant events must keep their author attribution")

        # The membership row is preserved (status='left') under the ROOM's tenant.
        with self.service.backend.transaction() as tx:
            row = tx.execute(
                "SELECT tenant_id, status FROM cloud_room_members "
                "WHERE room_id = ? AND agent_id = ?",
                (s["room"]["room_id"], s["key_a"]["key_id"]),
            ).fetchone()
        self.assertIsNotNone(row, "the cross-tenant membership row must not be deleted")
        self.assertEqual(row["status"], "left",
                         "a revoked cross-tenant key's membership is marked left, not deleted")
        self.assertEqual(row["tenant_id"], s["x_tenant"],
                         "the membership lives under the ROOM's tenant, not the key owner's")


class OrgOffboardingSeatReleaseTests(unittest.TestCase):
    """Org offboarding (remove_member) must release the account's seats too."""

    def setUp(self) -> None:
        self.backend = SqliteWalBackend(tempfile.mkstemp(suffix=".db")[1])
        self.backend.initialize()
        ensure_schema(self.backend)
        self.tenant_id = "tenant_offboard"
        self.backend.create_tenant(self.tenant_id, "Offboard Org")
        self.owner_id, _ = accounts.signup(
            self.backend, self.tenant_id, "owner@example.com", "password-123")
        self._membership("owner", self.owner_id)
        self.member_id, _ = accounts.signup(
            self.backend, self.tenant_id, "member@example.com", "password-123")
        self._membership("member", self.member_id)

    def tearDown(self) -> None:
        self.backend.close()

    def _membership(self, role: str, account_id: str) -> None:
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(tenant_id, account_id) DO UPDATE SET role = ?",
                (self.tenant_id, account_id, role, "now", role),
            )
            tx.commit()

    def test_org_offboarding_releases_the_removed_accounts_seats(self) -> None:
        rooms = CloudRoomService(self.backend)
        _sid, owner_raw = sessions.create(self.backend, self.tenant_id, self.owner_id, role="owner")
        owner_ctx = sessions.validate(self.backend, owner_raw)
        _msid, member_raw = sessions.create(self.backend, self.tenant_id, self.member_id, role="member")

        created = rooms.create_room(self.tenant_id, self.owner_id, member_raw, cap=3, name="offboard")
        room_id = created["room_id"]
        link = created["link_token"]

        # The member joins under its session identity AND under an agent key.
        rooms.join_room(self.tenant_id, room_id, link, self.member_id, True, member_raw)
        key_id, raw_key = agent_keys.create(self.backend, self.tenant_id, self.member_id, "ci")
        key_ctx = agent_keys.validate(self.backend, raw_key)
        rooms.join_room(self.tenant_id, room_id, link, key_ctx.agent_id, True, member_raw)

        info = rooms.room_info(self.tenant_id, room_id, self.owner_id)
        self.assertEqual(info["member_count"], 3)

        # Offboarding revokes the account's keys AND releases its seats.
        orgs.remove_member(owner_ctx, self.member_id)

        info = rooms.room_info(self.tenant_id, room_id, self.owner_id)
        self.assertEqual(info["member_count"], 1,
                         "org offboarding must release the removed member's seats")
        member_ids = {m["agent_id"] for m in info["members"]}
        self.assertNotIn(self.member_id, member_ids)
        self.assertNotIn(key_ctx.agent_id, member_ids)

        # The removed member's key is dead.
        with self.assertRaises(AuthError) as cm:
            agent_keys.validate(self.backend, raw_key)
        self.assertEqual(cm.exception.args[0], "invalid_session")

        # A brand-new identity can take a freed seat.
        fresh_key_id, fresh_raw = agent_keys.create(self.backend, self.tenant_id, self.owner_id, "fresh")
        fresh_ctx = agent_keys.validate(self.backend, fresh_raw)
        joined = rooms.join_room(self.tenant_id, room_id, link, fresh_ctx.agent_id, True, owner_raw)
        self.assertEqual(joined["status"], "active")
        info = rooms.room_info(self.tenant_id, room_id, self.owner_id)
        self.assertEqual(info["member_count"], 2)

        # The freed rows are preserved as 'left' — history intact.
        with self.backend.transaction() as tx:
            rows = tx.execute(
                "SELECT agent_id, status FROM cloud_room_members "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id IN (?, ?)",
                (self.tenant_id, room_id, self.member_id, key_ctx.agent_id),
            ).fetchall()
        self.assertEqual(len(rows), 2, "offboarding must preserve the membership rows")
        self.assertTrue(all(r["status"] == "left" for r in rows))


class CrossTenantOrgOffboardingSeatReleaseTests(unittest.TestCase):
    """remove_member must release the removed account's seats in ANOTHER tenant's room."""

    def setUp(self) -> None:
        self.backend = SqliteWalBackend(tempfile.mkstemp(suffix=".db")[1])
        self.backend.initialize()
        ensure_schema(self.backend)
        # Tenant X: owns the room the offboarded account has joined.
        self.tenant_x = "tenant_xroom"
        self.backend.create_tenant(self.tenant_x, "Room Org")
        self.owner_x, _ = accounts.signup(self.backend, self.tenant_x, "owner@xroom.example.com", "password-123")
        self._membership(self.tenant_x, "owner", self.owner_x)
        # Tenant Y: the offboarded account's own org.
        self.tenant_y = "tenant_yoff"
        self.backend.create_tenant(self.tenant_y, "Offboarded Org")
        self.owner_y, _ = accounts.signup(self.backend, self.tenant_y, "owner@yoff.example.com", "password-123")
        self._membership(self.tenant_y, "owner", self.owner_y)
        self.member_y, _ = accounts.signup(self.backend, self.tenant_y, "member@yoff.example.com", "password-123")
        self._membership(self.tenant_y, "member", self.member_y)

    def tearDown(self) -> None:
        self.backend.close()

    def _membership(self, tenant_id: str, role: str, account_id: str) -> None:
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(tenant_id, account_id) DO UPDATE SET role = ?",
                (tenant_id, account_id, role, "now", role),
            )
            tx.commit()

    def test_org_offboarding_releases_cross_tenant_seats(self) -> None:
        rooms = CloudRoomService(self.backend)
        _x_sid, owner_x_raw = sessions.create(self.backend, self.tenant_x, self.owner_x, role="owner")
        _y_sid, owner_y_raw = sessions.create(self.backend, self.tenant_y, self.owner_y, role="owner")
        owner_y_ctx = sessions.validate(self.backend, owner_y_raw)
        _m_sid, member_y_raw = sessions.create(self.backend, self.tenant_y, self.member_y, role="member")

        created = rooms.create_room(self.tenant_x, self.owner_x, owner_x_raw, cap=3, name="cross-offboard")
        room_id = created["room_id"]
        link = created["link_token"]

        # The tenant-Y account joins the tenant-X room under its account AND a key.
        rooms.join_room(self.tenant_y, room_id, link, self.member_y, True, member_y_raw)
        key_id, raw_key = agent_keys.create(self.backend, self.tenant_y, self.member_y, "ci")
        key_ctx = agent_keys.validate(self.backend, raw_key)
        rooms.join_room(self.tenant_y, room_id, link, key_ctx.agent_id, True, member_y_raw)

        info = rooms.room_info(self.tenant_x, room_id, self.owner_x)
        self.assertEqual(info["member_count"], 3)

        # Offboarding the account from its OWN org must release its seats in the
        # other org's room too.
        orgs.remove_member(owner_y_ctx, self.member_y)

        info = rooms.room_info(self.tenant_x, room_id, self.owner_x)
        self.assertEqual(info["member_count"], 1,
                         "cross-tenant org offboarding must release the removed account's seats")
        member_ids = {m["agent_id"] for m in info["members"]}
        self.assertNotIn(self.member_y, member_ids)
        self.assertNotIn(key_ctx.agent_id, member_ids)

        # The removed account's key is dead.
        with self.assertRaises(AuthError) as cm:
            agent_keys.validate(self.backend, raw_key)
        self.assertEqual(cm.exception.args[0], "invalid_session")

        # A brand-new identity can take a freed seat.
        fresh_key_id, fresh_raw = agent_keys.create(self.backend, self.tenant_x, self.owner_x, "fresh")
        fresh_ctx = agent_keys.validate(self.backend, fresh_raw)
        joined = rooms.join_room(self.tenant_x, room_id, link, fresh_ctx.agent_id, True, owner_x_raw)
        self.assertEqual(joined["status"], "active")
        info = rooms.room_info(self.tenant_x, room_id, self.owner_x)
        self.assertEqual(info["member_count"], 2)

        # The freed rows are preserved as 'left' under the ROOM's tenant — history intact.
        with self.backend.transaction() as tx:
            rows = tx.execute(
                "SELECT agent_id, status, tenant_id FROM cloud_room_members "
                "WHERE room_id = ? AND agent_id IN (?, ?)",
                (room_id, self.member_y, key_ctx.agent_id),
            ).fetchall()
        self.assertEqual(len(rows), 2, "offboarding must preserve the membership rows")
        self.assertTrue(all(r["status"] == "left" for r in rows))
        self.assertTrue(all(r["tenant_id"] == self.tenant_x for r in rows),
                        "the preserved rows live under the ROOM's tenant")


if __name__ == "__main__":
    unittest.main()
