"""BUG 1 — a FAILED join permanently burned a room-member slot (exploitable DoS).

``join_room`` used to run the plan-member-cap check/increment in its OWN
transaction and the room-cap check + membership insert in a SEPARATE one. A
``room_full`` refusal rolled back only the second transaction, so the counter
increment survived: an attacker holding ONLY the share link could spam joins
into a full room and drive ``cloud_room_counters.members`` up to the plan cap,
permanently bricking the room for genuine joins.

Fix under test: the plan-cap check, the counter increment, the room-cap check
and the membership insert all run in ONE transaction, so a refused join leaves
no trace and concurrent joins cannot oversubscribe a room.

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

FREE_MAX_MEMBERS_PER_ROOM = 15


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


class QuotaCounterBug1Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="finalisma-quota-bug1-")
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

    def _create(self, token: str, cap: int = 10) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/create", {"cap": cap}, token)

    def _join(self, token: str, room_id: str, link_token: str) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/join", {
            "room_id": room_id, "link_token": link_token,
            "consent": True,
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


class TestFailedJoinsDoNotBurnSlots(QuotaCounterBug1Base):
    """The DoS: repeated refused joins must not change the counter, and a room
    that is genuinely full must refuse with room_full — never quota_exceeded —
    no matter how many refused joins precede it."""

    def test_spam_room_full_joins_leave_counter_untouched(self) -> None:
        owner = self._signup("dos-owner@example.com")
        otok, oten = owner["session_token"], owner["tenant_id"]

        status, room = self._create(otok, cap=2)
        self.assertEqual(status, 201)
        rid = room["room_id"]

        # Owner + one DISTINCT account fill the room to its declared cap of 2.
        a = self._signup("dos-agent-a@example.com")
        status, body = self._join(a["session_token"], rid, room["link_token"])
        self.assertEqual(status, 200, f"join agent-a failed: {body}")
        self.assertEqual(self._member_counter(oten, rid), 2)

        # An attacker holding ONLY the share link spams 8 joins with 8 fresh
        # identities. All must be refused room_full.
        for i in range(8):
            spam = self._signup(f"dos-attacker-{i}@example.com")
            status, body = self._join(spam["session_token"], rid, room["link_token"])
            self.assertEqual(status, 409, f"spam join {i} should be room_full: {body}")
            self.assertEqual(body["error"]["code"], "room_full")

        # The counter equals the real membership — the spam burned nothing.
        self.assertEqual(self._member_counter(oten, rid), 2)
        self.assertEqual(self._member_counter(oten, rid), self._active_members(oten, rid))

        # A genuine join while the room is genuinely full is refused with
        # room_full — NOT quota_exceeded. Before the fix, the spam had driven
        # the counter to the plan cap, so this join was refused quota_exceeded
        # and the room was permanently bricked.
        genuine = self._signup("dos-genuine@example.com")
        status, body = self._join(genuine["session_token"], rid, room["link_token"])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "room_full")


class TestConcurrentJoinsOneSeat(QuotaCounterBug1Base):
    """Concurrency — N simultaneous joins racing the last seat: exactly one
    succeeds and the counter equals the real membership count afterwards."""

    def test_one_of_n_concurrent_joins_wins_and_counter_matches(self) -> None:
        owner = self._signup("race1-owner@example.com")
        otok, oten = owner["session_token"], owner["tenant_id"]

        status, room = self._create(otok, cap=FREE_MAX_MEMBERS_PER_ROOM)
        self.assertEqual(status, 201)
        rid = room["room_id"]

        # Owner + 8 DISTINCT accounts = 9 active members, one slot left (cap 10).
        for i in range(FREE_MAX_MEMBERS_PER_ROOM - 2):
            tok = self._signup(f"race1-pre-{i}@example.com")["session_token"]
            status, body = self._join(tok, rid, room["link_token"])
            self.assertEqual(status, 200, f"pre-join {i} failed: {body}")

        results: list[tuple[int, dict]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(5)

        def attempt(i: int) -> None:
            # Each racer is a FRESH account — five distinct identities contend
            # for the single remaining seat.
            tok = self._signup(f"race1-fresh-{i}@example.com")["session_token"]
            barrier.wait()
            s, body = self._join(tok, rid, room["link_token"])
            with lock:
                results.append((s, body))

        threads = [
            threading.Thread(target=attempt, args=(i,)) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        statuses = [s for s, _ in results]
        self.assertEqual(len(results), 5, f"expected 5 responses, got {results}")
        self.assertEqual(
            statuses.count(200), 1,
            f"exactly ONE concurrent join may succeed, got {statuses}",
        )
        for s, body in results:
            if s != 200:
                self.assertEqual(body["error"]["code"], "quota_exceeded")

        # The counter equals the real membership — no drift under contention.
        self.assertEqual(self._member_counter(oten, rid), self._active_members(oten, rid))
        self.assertEqual(self._member_counter(oten, rid), FREE_MAX_MEMBERS_PER_ROOM)


if __name__ == "__main__":
    unittest.main()
