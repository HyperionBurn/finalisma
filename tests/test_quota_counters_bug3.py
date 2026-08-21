"""BUG 3 — max_events_per_month was dead code.

The field was defined in PlanLimits but nothing read it: with the plan pinned
to max_events_per_month=1, 8 of 8 messages were accepted. Meanwhile the public
pricing page advertises "10,000 events / month" (free) and "100,000 events /
month" (pro), so the field is a published promise, not an internal detail.

Fix chosen: wire it to a monthly cloud_counters bucket and enforce it. A
per-calendar-month bucket (``events:<YYYY-MM>``) makes it a MONTHLY budget
rather than a lifetime one. The check and the increment run in the SAME
transaction as the event append, so a refused message leaves no counter trace.

What counts as an "event": a message appended to a room (room.message). The
product's monthly event budget is its agent-to-agent messaging, not lifecycle
bookkeeping.

Drives the REAL /v1 HTTP entry points; counter reads are assertion only.
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

from weft_cloud.quotas import PlanLimits, PLANS, events_month_bucket
from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


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


class TestMonthlyEventLimitEnforced(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="finalisma-quota-bug3-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.service = WeftCloudService(SqliteWalBackend(self.db_path))
        _CloudHTTPHandler.service = self.service
        self.server = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self.server.start()
        # Pin the FREE plan's monthly event budget to 1 for this test. The
        # other limits (5 rooms, 15 members, 60 msgs/min) stay at their
        # published values.
        self._orig_plan = PLANS["free"]
        PLANS["free"] = PlanLimits(max_events_per_month=1)

    def tearDown(self) -> None:
        PLANS["free"] = self._orig_plan
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

    def test_events_over_monthly_budget_refused(self) -> None:
        owner = self._signup("events-owner@example.com")
        tok, ten = owner["session_token"], owner["tenant_id"]

        status, room = _post(self.base, "/v1/rooms/create", {"cap": 6}, tok)
        self.assertEqual(status, 201)
        rid = room["room_id"]

        def send(text: str) -> tuple[int, dict]:
            return _post(self.base, "/v1/rooms/send", {
                "room_id": rid, "target_spec": "*", "payload": {"text": text},
            }, tok)

        # The first message is accepted...
        status, body = send("m-1")
        self.assertEqual(status, 200, f"first message must be accepted: {body}")

        # ...and the second is refused once the month's budget of 1 is spent.
        status, body = send("m-2")
        self.assertEqual(status, 409, f"second message must be refused: {body}")
        self.assertEqual(body["error"]["code"], "quota_exceeded")
        self.assertEqual(body["error"]["limit"]["name"], "max_events_per_month")
        self.assertEqual(body["error"]["limit"]["value"], 1)
        self.assertEqual(body["error"]["limit"]["plan"], "free")

        # The monthly bucket recorded exactly one event.
        with self.service.backend.transaction() as tx:
            row = tx.execute(
                "SELECT value FROM cloud_counters WHERE tenant_id=? AND counter=?",
                (ten, events_month_bucket()),
            ).fetchone()
        self.assertIsNotNone(row, "monthly event bucket must exist")
        self.assertEqual(int(row["value"]), 1)


if __name__ == "__main__":
    unittest.main()
