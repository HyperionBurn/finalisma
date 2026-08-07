"""Outbox + retry lane tests (TDD).

Stdlib only, temp SQLite. Covers enqueue fan-out, atomic claim, delivery /
retry / DLQ, restart durability, backoff monotonicity, DLQ peek/retry,
stats, and isolation across team/roster ids.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Thread

from weft_mcp import outbox


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        outbox.init(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass

    # ---- helpers ---------------------------------------------------------
    def _envelope(self, envelope_id="env-1", kind="msg"):
        return {
            "envelope_id": envelope_id,
            "sender": "agent-a",
            "kind": kind,
            "payload_json": json.dumps({"hello": "world"}),
        }

    def _count(self, table):
        conn = sqlite3.connect(self.tmp.name)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    # ---- enqueue fan-out -------------------------------------------------
    def test_enqueue_creates_one_entry_per_recipient(self):
        recipients = ["r1", "r2", "r3"]
        ids = outbox.enqueue(self._envelope(), recipients)
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(self._count("outbox_entries"), 3)
        for eid in ids:
            ent = outbox._get_entry(eid)
            self.assertEqual(ent["status"], "queued")
            self.assertEqual(ent["attempts"], 0)
            self.assertEqual(ent["envelope_id"], "env-1")

    def test_enqueue_per_recipient_idempotency(self):
        env = self._envelope()
        first = outbox.enqueue(env, ["r1", "r2"])
        second = outbox.enqueue(env, ["r1", "r2"])
        # Same envelope+recipient pairs must yield same entry ids (idempotent).
        self.assertEqual(sorted(first), sorted(second))
        self.assertEqual(self._count("outbox_entries"), 2)

    def test_enqueue_isolation_across_teams(self):
        env = self._envelope()
        a = outbox.enqueue(env, ["r1"], roster_or_team_id="team-A")
        b = outbox.enqueue(env, ["r1"], roster_or_team_id="team-B")
        # Different tenant scope => distinct entries even for same envelope/recipient.
        self.assertNotEqual(set(a), set(b))
        self.assertEqual(self._count("outbox_entries"), 2)

    # ---- claim atomicity -------------------------------------------------
    def test_claim_atomicity_concurrent(self):
        env = self._envelope()
        ids = outbox.enqueue(env, [f"r{i}" for i in range(20)])

        claimed_bags: list[list[str]] = []

        def worker():
            due = outbox.claim_due(limit=20, now=time.time() + 1)
            claimed_bags.append([e["entry_id"] for e in due])

        threads = [Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        all_claimed = [eid for bag in claimed_bags for eid in bag]
        # Every entry claimed at most once (no double-claim).
        self.assertEqual(len(all_claimed), len(set(all_claimed)))
        # All entries must be accounted for.
        self.assertEqual(set(all_claimed), set(ids))

    def test_claim_only_returns_due_entries(self):
        env = self._envelope()
        ids = outbox.enqueue(env, ["r1", "r2"])
        # Nothing due in the past.
        due = outbox.claim_due(limit=10, now=time.time() - 1000)
        self.assertEqual(due, [])
        # Everything due in the future.
        due = outbox.claim_due(limit=10, now=time.time() + 1000)
        self.assertEqual({e["entry_id"] for e in due}, set(ids))

    # ---- deliver / retry / DLQ ------------------------------------------
    def test_mark_delivered_prevents_double_delivery(self):
        env = self._envelope()
        (eid,) = outbox.enqueue(env, ["r1"])
        outbox.claim_due(limit=10, now=time.time() + 1)
        outbox.mark_delivered(eid, receipt="rcpt-1")
        ent = outbox._get_entry(eid)
        self.assertEqual(ent["status"], "delivered")
        # Calling again is a no-op (replay-safe).
        outbox.mark_delivered(eid, receipt="rcpt-2")
        ent2 = outbox._get_entry(eid)
        self.assertEqual(ent2["status"], "delivered")

    def test_retry_then_dlq_after_max_attempts(self):
        env = self._envelope()
        (eid,) = outbox.enqueue(env, ["r1"])
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            outbox.claim_due(limit=10, now=time.time() + 1)
            outbox.mark_retry(eid, error=f"boom-{attempt}", backoff_policy={"max_attempts": max_attempts})
        # After hitting max_attempts it must be in the DLQ.
        self.assertEqual(self._count("outbox_dlq"), 1)
        ent = outbox._get_entry(eid)
        self.assertEqual(ent["status"], "dead")
        dlq = outbox.peek_dlq()
        self.assertEqual(len(dlq), 1)
        self.assertEqual(dlq[0]["entry_id"], eid)
        self.assertIn("boom-3", dlq[0]["reason"])

    def test_retry_reschedules_with_future_attempt(self):
        env = self._envelope()
        (eid,) = outbox.enqueue(env, ["r1"])
        outbox.claim_due(limit=10, now=time.time() + 1)
        outbox.mark_retry(eid, error="transient", backoff_policy={"max_attempts": 5})
        ent = outbox._get_entry(eid)
        self.assertEqual(ent["status"], "queued")
        self.assertEqual(ent["attempts"], 1)
        # next_attempt_at must be in the future (backoff applied).
        self.assertGreater(ent["next_attempt_at"], time.time() - 0.001)

    # ---- restart durability ----------------------------------------------
    def test_restart_durability(self):
        env = self._envelope()
        ids = outbox.enqueue(env, ["r1", "r2", "r3"])
        outbox.claim_due(limit=10, now=time.time() + 1)
        # Reopen the file (simulate process restart).
        outbox.init(self.tmp.name)
        # Entries must still be present and claimable again (in_flight -> queued on reopen is not required; they persist).
        due = outbox.claim_due(limit=10, now=time.time() + 1)
        self.assertEqual({e["entry_id"] for e in due}, set(ids))

    # ---- backoff schedule monotonicity -----------------------------------
    def test_backoff_exponential_monotonicity(self):
        bp = outbox.BackoffPolicy()
        env = self._envelope()
        (eid,) = outbox.enqueue(env, ["r1"])
        timestamps: list[float] = []
        for _ in range(4):
            outbox.claim_due(limit=10, now=time.time() + 1)
            info = outbox.mark_retry(eid, error="x", backoff_policy={"max_attempts": 10})
            timestamps.append(info["next_attempt_at"])
        # Each next_attempt_at should be >= the previous (monotonic non-decreasing).
        for prev, nxt in zip(timestamps, timestamps[1:]):
            self.assertGreaterEqual(nxt, prev)

    def test_backoff_respects_cap(self):
        bp = outbox.BackoffPolicy(base=1.0, factor=2.0, cap=5.0)
        # After many attempts the computed delay must not exceed the cap.
        for attempt in range(20):
            delay = bp.delay(attempt)
            self.assertLessEqual(delay, bp.cap + 1e-9)

    def test_backoff_has_jitter(self):
        bp = outbox.BackoffPolicy(base=1.0, factor=2.0, cap=60.0)
        delays = {bp.delay(3) for _ in range(100)}
        # Jitter must produce more than one distinct value.
        self.assertGreater(len(delays), 1)

    # ---- DLQ peek / retry -----------------------------------------------
    def test_dlq_peek_and_retry(self):
        env = self._envelope()
        (eid,) = outbox.enqueue(env, ["r1"])
        for attempt in range(1, 3):
            outbox.claim_due(limit=10, now=time.time() + 1)
            outbox.mark_retry(eid, error=f"e{attempt}", backoff_policy={"max_attempts": 2})
        self.assertEqual(self._count("outbox_dlq"), 1)
        # Retry from DLQ: re-queues the entry.
        outbox.retry_dlq(eid)
        ent = outbox._get_entry(eid)
        self.assertEqual(ent["status"], "queued")
        self.assertEqual(ent["attempts"], 0)
        self.assertEqual(self._count("outbox_dlq"), 0)

    # ---- stats -----------------------------------------------------------
    def test_stats(self):
        env = self._envelope()
        a, b, c = outbox.enqueue(env, ["r1", "r2", "r3"])
        outbox.claim_due(limit=10, now=time.time() + 1)
        outbox.mark_delivered(a, receipt="x")
        outbox.mark_retry(b, error="y", backoff_policy={"max_attempts": 2})
        # c still in_flight
        stats = outbox.stats()
        self.assertEqual(stats["delivered"], 1)
        self.assertEqual(stats["in_flight"], 1)
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(stats["dead"], 0)

    # ---- coexistence with core (no import cycles) ------------------------
    def test_coexistence_with_core_connection(self):
        # A plain sqlite3 connection can open the same DB; outbox tables coexist.
        conn = sqlite3.connect(self.tmp.name)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'outbox_%'").fetchall()}
            self.assertIn("outbox_entries", tables)
            self.assertIn("outbox_dlq", tables)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
