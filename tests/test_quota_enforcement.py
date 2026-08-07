"""Quota ENFORCEMENT integration tests — the gap was "unit-tested but never called".

The original defect: src/finalisma_cloud/quotas.py defined plan limits and
atomic enforcement, but /v1/rooms/create and /v1/rooms/join never consulted
them — a free tenant could create cap=64 rooms and join 40 agents. The module
was green in isolation, which is exactly how the defect survived.

Every test here drives the REAL HTTP entry points (/v1/rooms/create,
/v1/rooms/join, /v1/rooms/info) against a real ThreadingHTTPServer. Nothing
calls join_room_with_quota / create_room_with_quota directly, so each test
FAILS if the wiring is ever removed again:

  - the 11th-member test asserts the refusal code is quota_exceeded. Without
    the join_room_with_quota call site the 11th join would hit the room's own
    cap and return room_full instead (or succeed for cap > plan).
  - the 6th-room test asserts quota_exceeded; without the create call site it
    would succeed.
  - the cap-bound test asserts cap=11 is REJECTED; without the bound it would
    be echoed back as success.

Decision (item 2): a requested cap above the tenant's plan member limit is
REJECTED with a plan-aware quota_exceeded error, never silently clamped.
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finalisma_cloud.service import FinalismaCloudService, _CloudHTTPHandler
from finalisma_cloud.storage import SqliteWalBackend

# Published plan numbers, kept literal here so an accidental limit change is a
# test failure, not a silent pricing change. 5/10 free, 50/50 pro.
FREE_MAX_ROOMS = 5
FREE_MAX_MEMBERS_PER_ROOM = 10
PRO_MAX_MEMBERS_PER_ROOM = 50


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        return exc.code, payload


def _get(base: str, path: str, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        return exc.code, payload


class QuotaEnforcementTestBase(unittest.TestCase):
    """Real HTTP service on a background thread, one fresh DB per test."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="finalisma-quota-test-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        self.service = FinalismaCloudService(SqliteWalBackend(self.db_path))
        self.port = 18900 + (hash(self.tmpdir) % 1000)
        self.base = f"http://127.0.0.1:{self.port}"
        self._server_started = threading.Event()
        self.server = threading.Thread(target=self._serve, daemon=True)
        self.server.start()
        self._server_started.wait(timeout=5)

    def _serve(self) -> None:
        from http.server import ThreadingHTTPServer
        _CloudHTTPHandler.service = self.service
        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _CloudHTTPHandler)
        self._httpd = httpd
        self._server_started.set()
        httpd.serve_forever()

    def tearDown(self) -> None:
        if hasattr(self, "_httpd"):
            self._httpd.shutdown()
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

    def _set_plan(self, tenant_id: str, plan_id: str) -> None:
        """Promote/demote a tenant's plan at the storage seam.

        There is no public plan-upgrade endpoint in v1 (Wave I billing owns
        that); the enforcement reads cloud_tenants.plan_id through PLANS, so
        setting it here is exactly what a billing integration would do.
        """
        with self.service.backend.transaction() as tx:
            tx.execute(
                "UPDATE cloud_tenants SET plan_id = ? WHERE tenant_id = ?",
                (plan_id, tenant_id),
            )
            tx.commit()

    def _raw_create(self, token: str, cap: int, **kwargs) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/create", {"cap": cap, **kwargs}, token)

    def _raw_join(self, token: str, room_id: str, link_token: str,
                  agent_id: str) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/join", {
            "room_id": room_id,
            "link_token": link_token,
            "agent_id": agent_id,
            "consent": True,
        }, token)

    def _room_info(self, token: str, room_id: str, agent_id: str) -> tuple[int, dict]:
        from urllib.parse import urlencode
        return _get(self.base, f"/v1/rooms/info?{urlencode({'room_id': room_id, 'agent_id': agent_id})}",
                    token)


class TestMemberCapEnforced(QuotaEnforcementTestBase):
    """The 11th member of a free tenant's room is refused with quota_exceeded."""

    def test_eleventh_member_refused_with_quota_exceeded(self) -> None:
        owner = self._signup("cap-owner@example.com", "CorrectHorse!1")
        fleet = self._signup("cap-fleet@example.com", "CorrectHorse!1")

        status, room = self._raw_create(owner["session_token"], cap=FREE_MAX_MEMBERS_PER_ROOM)
        self.assertEqual(status, 201)
        self.assertEqual(room["cap"], FREE_MAX_MEMBERS_PER_ROOM)

        # Owner auto-joins as member 1; 9 more fill the room to the cap of 10.
        for i in range(FREE_MAX_MEMBERS_PER_ROOM - 1):
            s, body = self._raw_join(
                fleet["session_token"], room["room_id"], room["link_token"], f"agent-{i}")
            self.assertEqual(s, 200, f"join {i} should succeed: {body}")

        # The 11th member is refused by the PLAN cap, not the room's own cap.
        s, body = self._raw_join(
            fleet["session_token"], room["room_id"], room["link_token"], "agent-last")
        self.assertEqual(s, 409)
        self.assertEqual(body["error"]["code"], "quota_exceeded")
        self.assertEqual(body["error"]["limit"]["name"], "max_members_per_room")
        self.assertEqual(body["error"]["limit"]["value"], FREE_MAX_MEMBERS_PER_ROOM)
        self.assertEqual(body["error"]["limit"]["plan"], "free")

        # The roster is still exactly at the cap — nothing oversubscribed.
        s, info = self._room_info(owner["session_token"], room["room_id"], owner["account_id"])
        self.assertEqual(s, 200)
        self.assertEqual(info["member_count"], FREE_MAX_MEMBERS_PER_ROOM)


class TestRoomCountEnforced(QuotaEnforcementTestBase):
    """A free tenant cannot create a 6th room."""

    def test_sixth_room_refused_with_quota_exceeded(self) -> None:
        owner = self._signup("rooms-owner@example.com", "CorrectHorse!1")
        for i in range(FREE_MAX_ROOMS):
            s, body = self._raw_create(owner["session_token"], cap=6, name=f"room-{i}")
            self.assertEqual(s, 201, f"room {i} should be created: {body}")

        s, body = self._raw_create(owner["session_token"], cap=6, name="room-over")
        self.assertEqual(s, 409)
        self.assertEqual(body["error"]["code"], "quota_exceeded")
        self.assertEqual(body["error"]["limit"]["name"], "max_rooms")
        self.assertEqual(body["error"]["limit"]["value"], FREE_MAX_ROOMS)
        self.assertEqual(body["error"]["limit"]["plan"], "free")


class TestCapBoundedByPlan(QuotaEnforcementTestBase):
    """A free tenant cannot create a room whose cap exceeds the plan (REJECT)."""

    def test_cap_above_plan_rejected(self) -> None:
        owner = self._signup("capbound-owner@example.com", "CorrectHorse!1")
        s, body = self._raw_create(owner["session_token"], cap=FREE_MAX_MEMBERS_PER_ROOM + 1)
        self.assertEqual(s, 409)
        self.assertEqual(body["error"]["code"], "quota_exceeded")
        self.assertEqual(body["error"]["limit"]["name"], "max_members_per_room")
        self.assertEqual(body["error"]["limit"]["value"], FREE_MAX_MEMBERS_PER_ROOM)
        self.assertEqual(body["error"]["limit"]["plan"], "free")

    def test_cap_at_exactly_the_plan_limit_accepted(self) -> None:
        owner = self._signup("capok-owner@example.com", "CorrectHorse!1")
        s, body = self._raw_create(owner["session_token"], cap=FREE_MAX_MEMBERS_PER_ROOM)
        self.assertEqual(s, 201)
        self.assertEqual(body["cap"], FREE_MAX_MEMBERS_PER_ROOM)


class TestProPlanHonored(QuotaEnforcementTestBase):
    """A pro tenant gets the higher limits through the same HTTP path."""

    def test_pro_member_and_room_and_cap_limits(self) -> None:
        owner = self._signup("pro-owner@example.com", "CorrectHorse!1")
        self._set_plan(owner["tenant_id"], "pro")

        # Cap bound honors pro: cap=50 is accepted (free would reject >10).
        s, room = self._raw_create(owner["session_token"], cap=PRO_MAX_MEMBERS_PER_ROOM)
        self.assertEqual(s, 201)
        self.assertEqual(room["cap"], PRO_MAX_MEMBERS_PER_ROOM)

        # Member cap honors pro: 11 members (owner + 11) sails past the free
        # cap of 10, joined cross-tenant via the link from a free fleet tenant.
        fleet = self._signup("pro-fleet@example.com", "CorrectHorse!1")
        for i in range(FREE_MAX_MEMBERS_PER_ROOM + 1):
            s, body = self._raw_join(
                fleet["session_token"], room["room_id"], room["link_token"], f"pro-agent-{i}")
            self.assertEqual(s, 200, f"pro join {i} should succeed: {body}")

        s, info = self._room_info(owner["session_token"], room["room_id"], owner["account_id"])
        self.assertEqual(s, 200)
        self.assertEqual(info["member_count"], FREE_MAX_MEMBERS_PER_ROOM + 2)  # owner + 11

        # Room-count cap honors pro: 6 rooms total (free cap is 5).
        for i in range(FREE_MAX_ROOMS + 1):
            s, body = self._raw_create(owner["session_token"], cap=6, name=f"pro-room-{i}")
            self.assertEqual(s, 201, f"pro room {i} should be created: {body}")


class TestAtomicMemberCapUnderConcurrency(QuotaEnforcementTestBase):
    """Exactly one of N concurrent joins at the last slot succeeds."""

    def test_concurrent_joins_at_last_slot_admit_exactly_one(self) -> None:
        owner = self._signup("race-owner@example.com", "CorrectHorse!1")
        fleet = self._signup("race-fleet@example.com", "CorrectHorse!1")

        status, room = self._raw_create(owner["session_token"], cap=FREE_MAX_MEMBERS_PER_ROOM)
        self.assertEqual(status, 201)

        # Owner + 8 agents = 9 active members, one slot left (cap 10).
        for i in range(FREE_MAX_MEMBERS_PER_ROOM - 2):
            s, body = self._raw_join(
                fleet["session_token"], room["room_id"], room["link_token"], f"pre-{i}")
            self.assertEqual(s, 200, f"pre-join {i} failed: {body}")

        results: list[tuple[int, dict]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(5)

        def attempt(agent_id: str) -> None:
            barrier.wait()
            s, body = self._raw_join(
                fleet["session_token"], room["room_id"], room["link_token"], agent_id)
            with lock:
                results.append((s, body))

        threads = [
            threading.Thread(target=attempt, args=(f"race-{i}",)) for i in range(5)
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
                self.assertEqual(s, 409, f"loser should be 409, got {s}: {body}")
                self.assertEqual(body["error"]["code"], "quota_exceeded")

        # The roster reflects exactly the cap — the atomic gate oversubscribed
        # nothing.
        s, info = self._room_info(owner["session_token"], room["room_id"], owner["account_id"])
        self.assertEqual(s, 200)
        self.assertEqual(info["member_count"], FREE_MAX_MEMBERS_PER_ROOM)


if __name__ == "__main__":
    unittest.main()
