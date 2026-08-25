"""BUG 2 — counters never decremented, so the free plan was 5 rooms LIFETIME.

``close_room`` and ``leave_room`` did not touch the counters. A tenant that
created 5 rooms and closed all 5 was still at the 5-room cap forever; a member
that left a room never freed its slot.

Design chosen here: counters track ACTIVE state, not lifetime creations —
  - ``cloud_counters.rooms`` == COUNT(cloud_rooms WHERE state != 'closed'),
    so closing releases a room back to the tenant's quota.
  - ``cloud_room_counters.members`` == COUNT(active members), so leaving frees
    a seat and reactivating a left member re-counts it.
This is the definition the invariant test enforces. Counters are decremented
with MAX(0, value - 1) so they can never go negative.

Drives the REAL /v1 HTTP entry points; counter reads are assertion only.
"""

from __future__ import annotations
from tests._server_readiness import await_serving as _await_serving

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

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend

FREE_MAX_ROOMS = 5


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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


class QuotaCounterBug2Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="finalisma-quota-bug2-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.service = WeftCloudService(SqliteWalBackend(self.db_path))
        _CloudHTTPHandler.service = self.service
        self.server = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self.server.start()
        _await_serving(self._httpd)

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

    def _signup(self, email: str) -> dict:
        status, body = _post(self.base, "/v1/auth/signup", {
            "email": email, "password": "CorrectHorse!1",
        })
        self.assertEqual(status, 201, f"signup failed: {body}")
        return body

    def _create(self, token: str, cap: int = 10, name: str | None = None) -> tuple[int, dict]:
        body = {"cap": cap}
        if name:
            body["name"] = name
        return _post(self.base, "/v1/rooms/create", body, token)

    def _join(self, token: str, room_id: str, link_token: str) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/join", {
            "room_id": room_id, "link_token": link_token,
            "consent": True,
        }, token)

    def _leave(self, token: str, room_id: str) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/leave", {
            "room_id": room_id,
        }, token)

    def _close(self, token: str, room_id: str) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/close", {
            "room_id": room_id,
        }, token)

    def _member_counter(self, tenant_id: str, room_id: str) -> int:
        with self.service.backend.transaction() as tx:
            row = tx.execute(
                "SELECT value FROM cloud_room_counters WHERE tenant_id=? AND room_id=? AND counter='members'",
                (tenant_id, room_id),
            ).fetchone()
        return int(row["value"]) if row else 0

    def _active_members(self, tenant_id: str, room_id: str) -> int:
        with self.service.backend.transaction() as tx:
            row = tx.execute(
                "SELECT COUNT(*) AS c FROM cloud_room_members WHERE tenant_id=? AND room_id=? AND status='active'",
                (tenant_id, room_id),
            ).fetchone()
        return int(row["c"])

    def _rooms_counter(self, tenant_id: str) -> int:
        with self.service.backend.transaction() as tx:
            row = tx.execute(
                "SELECT value FROM cloud_counters WHERE tenant_id=? AND counter='rooms'", (tenant_id,)
            ).fetchone()
        return int(row["value"]) if row else 0

    def _active_rooms(self, tenant_id: str) -> int:
        with self.service.backend.transaction() as tx:
            row = tx.execute(
                "SELECT COUNT(*) AS c FROM cloud_rooms WHERE tenant_id=? AND state != 'closed'", (tenant_id,)
            ).fetchone()
        return int(row["c"])

    def assert_counters_match_rows(self, tenant_id: str) -> None:
        """THE invariant: stored counters == actual row counts after any mixed
        sequence of joins, failed joins, leaves, rejoins and closes."""
        self.assertEqual(
            self._rooms_counter(tenant_id), self._active_rooms(tenant_id),
            "tenant rooms counter drifted from the count of active rooms",
        )
        with self.service.backend.transaction() as tx:
            rooms = tx.execute(
                "SELECT room_id FROM cloud_rooms WHERE tenant_id=?", (tenant_id,)
            ).fetchall()
        for row in rooms:
            room_id = row["room_id"]
            self.assertEqual(
                self._member_counter(tenant_id, room_id),
                self._active_members(tenant_id, room_id),
                f"room {room_id} member counter drifted from its active member count",
            )


class TestLeaveFreesMemberSlot(QuotaCounterBug2Base):
    """Leaving a room decrements the member counter and frees a seat; a later
    join succeeds; a left member reactivating re-counts only when a seat is
    genuinely free."""

    def test_leave_frees_seat_and_reactivation_is_gated(self) -> None:
        owner = self._signup("leave-owner@example.com")
        otok, oten = owner["session_token"], owner["tenant_id"]

        status, room = self._create(otok, cap=4, name="leave")
        self.assertEqual(status, 201)
        rid = room["room_id"]
        lk = room["link_token"]

        # Three DISTINCT accounts a, b, c join.
        tokens = {name: self._signup(f"leave-{name}@example.com")["session_token"]
                  for name in ("a", "b", "c")}
        for name in ("a", "b", "c"):
            status, body = self._join(tokens[name], rid, lk)
            self.assertEqual(status, 200, f"join {name}: {body}")

        # Room is full (owner + a + b + c = 4).
        self.assertEqual(self._member_counter(oten, rid), 4)

        # a leaves — the seat is freed and the counter follows the member.
        status, _ = self._leave(tokens["a"], rid)
        self.assertEqual(status, 200)
        self.assertEqual(self._member_counter(oten, rid), 3)
        self.assertEqual(self._member_counter(oten, rid), self._active_members(oten, rid))

        # d (a fresh account) takes the freed seat.
        d_tok = self._signup("leave-d@example.com")["session_token"]
        status, body = self._join(d_tok, rid, lk)
        self.assertEqual(status, 200, f"join d failed: {body}")
        self.assertEqual(self._member_counter(oten, rid), 4)

        # a tries to reactivate — the seat is gone, so the room refuses. This
        # must NOT oversubscribe the room or leave a counter trace.
        status, body = self._join(tokens["a"], rid, lk)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "room_full")
        self.assertEqual(self._member_counter(oten, rid), self._active_members(oten, rid))

        # b leaves, freeing a seat; b reactivates into it.
        self.assertEqual(self._leave(tokens["b"], rid)[0], 200)
        self.assertEqual(self._member_counter(oten, rid), 3)
        status, body = self._join(tokens["b"], rid, lk)
        self.assertEqual(status, 200, f"reactivation must succeed: {body}")
        self.assertEqual(self._member_counter(oten, rid), 4)
        self.assertEqual(self._member_counter(oten, rid), self._active_members(oten, rid))

        self.assert_counters_match_rows(oten)


class TestCloseFreesRoomQuota(QuotaCounterBug2Base):
    """Closing rooms releases room quota: create/close/create works on the free
    plan's 5-room cap."""

    def test_close_all_then_create_again(self) -> None:
        owner = self._signup("reuse-owner@example.com")
        tok, ten = owner["session_token"], owner["tenant_id"]

        created = []
        for i in range(FREE_MAX_ROOMS):
            status, body = self._create(tok, cap=6, name=f"r-{i}")
            self.assertEqual(status, 201, f"room {i} should be created: {body}")
            created.append(body)

        self.assertEqual(self._rooms_counter(ten), FREE_MAX_ROOMS)
        self.assertEqual(self._rooms_counter(ten), self._active_rooms(ten))

        for body in created:
            status, _ = self._close(tok, body["room_id"])
            self.assertEqual(status, 200)

        # Closing released every room back to the tenant's ACTIVE-room quota.
        self.assertEqual(self._rooms_counter(ten), 0)
        self.assertEqual(self._rooms_counter(ten), self._active_rooms(ten))

        # The 6th room now succeeds — the plan is not 5 rooms LIFETIME.
        status, body = self._create(tok, cap=6, name="sixth")
        self.assertEqual(status, 201, f"6th room after closing must succeed: {body}")
        self.assertEqual(self._rooms_counter(ten), 1)
        self.assert_counters_match_rows(ten)


class TestCounterInvariant(QuotaCounterBug2Base):
    """THE invariant: after a mixed sequence of joins, failed joins, leaves,
    rejoins and closes, the stored counters equal the actual row counts."""

    def test_invariant_after_mixed_sequence(self) -> None:
        owner = self._signup("inv-owner@example.com")
        otok, oten = owner["session_token"], owner["tenant_id"]

        status, room1 = self._create(otok, cap=4, name="room1")
        self.assertEqual(status, 201)
        status, room2 = self._create(otok, cap=5, name="room2")
        self.assertEqual(status, 201)
        r1, r2 = room1["room_id"], room2["room_id"]
        lk1 = room1["link_token"]

        # Three DISTINCT accounts join room1.
        tokens = {name: self._signup(f"inv-{name}@example.com")["session_token"]
                  for name in ("a", "b", "c")}
        for name in ("a", "b", "c"):
            status, body = self._join(tokens[name], r1, lk1)
            self.assertEqual(status, 200, f"join {name}: {body}")

        # A join past the room cap is refused room_full.
        d_tok = self._signup("inv-d@example.com")["session_token"]
        status, body = self._join(d_tok, r1, lk1)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "room_full")

        # b leaves; d takes the freed seat.
        self.assertEqual(self._leave(tokens["b"], r1)[0], 200)
        self.assertEqual(self._join(d_tok, r1, lk1)[0], 200)

        # b tries to rejoin — the seat is gone, so reactivation is refused
        # without oversubscribing or drifting the counter.
        status, body = self._join(tokens["b"], r1, lk1)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "room_full")
        self.assertEqual(self._member_counter(oten, r1), self._active_members(oten, r1))

        # a leaves and rejoins — a genuine reactivation restores the seat.
        self.assertEqual(self._leave(tokens["a"], r1)[0], 200)
        status, body = self._join(tokens["a"], r1, lk1)
        self.assertEqual(status, 200, f"reactivation must succeed: {body}")

        # Close room2, then create a third room to prove quota was freed.
        self.assertEqual(self._close(otok, r2)[0], 200)
        status, room3 = self._create(otok, cap=3, name="room3")
        self.assertEqual(status, 201)

        # The invariant holds everywhere: counters == rows.
        self.assert_counters_match_rows(oten)
        self.assertEqual(self._rooms_counter(oten), 2)  # room1 + room3 active
        self.assertEqual(self._active_members(oten, r1), 4)  # owner, a, c, d


if __name__ == "__main__":
    unittest.main()
