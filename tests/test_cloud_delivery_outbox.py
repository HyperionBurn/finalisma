"""Hosted delivery outbox lifecycle — the cloud_outbox completion state machine.

This pins the completion contract that mirrors the existing identity email
outbox worker (``identity/outbox_worker.py``):

  1. A drain worker claims due rows atomically with a lease; a dead worker's
     claim is reclaimed after ``lease_seconds``.
  2. Delivery marks the row terminal ``delivered``; a second drain never
     re-delivers.
  3. Transient failures retry with backoff; after ``max_attempts`` the row
     reaches the terminal dead-letter state ``dead``.
  4. A pluggable ``Deliverer`` (like the email ``Mailer``) performs the actual
     send; without one the worker exits cleanly and rows stay queued.
  5. ``cloud_010`` adds the lifecycle columns in place to existing databases,
     idempotently.

Tests drive the REAL ``CloudOutboxDrainer`` against real SQLite storage.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.migrations import apply_migrations
from weft_cloud.lease import LeaseHeartbeat
from weft_cloud.storage import SqliteWalBackend

from weft_cloud import delivery_worker  # noqa: E402


class _Permanent(delivery_worker.PermanentDeliveryError):
    pass


class _Transient(Exception):
    pass


class FakeDeliverer(delivery_worker.Deliverer):
    """Scripted deliverer: each deliver() pops the next outcome from ``script``.

    Outcomes: None (success), ``_Permanent`` (permanent failure),
    ``_Transient`` (transient failure). When the script is empty the call
    succeeds.
    """

    instances: list["FakeDeliverer"] = []
    script: list = []

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.script = []

    def __init__(self) -> None:
        self.sent: list[tuple] = []
        FakeDeliverer.instances.append(self)

    def deliver(self, tenant_id: str, recipient: str, payload_json: str) -> None:
        self.sent.append((tenant_id, recipient, payload_json))
        if FakeDeliverer.script:
            outcome = FakeDeliverer.script.pop(0)
            if isinstance(outcome, type) and issubclass(outcome, Exception):
                raise outcome()

    @classmethod
    def total_delivered(cls) -> int:
        return sum(len(inst.sent) for inst in cls.instances)


def make_backend() -> SqliteWalBackend:
    tmp = tempfile.TemporaryDirectory(prefix="cloud-delivery-")
    db_path = Path(tmp.name) / "cloud.db"
    backend = SqliteWalBackend(db_path)
    backend.initialize()
    apply_migrations(backend)
    backend._tmpdir = tmp  # type: ignore[attr-defined]
    return backend


class DeliveryDrainerTests(unittest.TestCase):
    """CloudOutboxDrainer lifecycle contract."""

    def setUp(self) -> None:
        FakeDeliverer.reset()
        self.backend = make_backend()
        self.tenant_id = "tenant_delivery"
        self.backend.create_tenant(self.tenant_id, "Delivery Tenant")

    def tearDown(self) -> None:
        try:
            self.backend.close()
        finally:
            tmp = getattr(self.backend, "_tmpdir", None)
            if tmp is not None:
                tmp.cleanup()

    def _seed(self, recipient: str = "agent-dest", payload: str = '{"seq":1}') -> str:
        return self.backend.enqueue_outbox(
            self.tenant_id, "env-1", recipient, payload
        )

    def _row(self, entry_id: str | None = None) -> dict:
        with self.backend.transaction() as tx:
            if entry_id is None:
                row = tx.execute("SELECT * FROM cloud_outbox").fetchone()
            else:
                row = tx.execute(
                    "SELECT * FROM cloud_outbox WHERE entry_id = ?", (entry_id,)
                ).fetchone()
        return dict(row)

    def _drainer(self, **kwargs) -> delivery_worker.CloudOutboxDrainer:
        return delivery_worker.CloudOutboxDrainer(
            self.backend, FakeDeliverer(), **kwargs
        )

    # -- 1. exactly-once + lease -------------------------------------------

    def test_drain_delivers_undelivered_row_exactly_once(self) -> None:
        entry_id = self._seed()
        drainer = self._drainer()
        first = drainer.drain_once()
        self.assertEqual(first["sent"], 1)
        self.assertEqual(first["claimed"], 1)
        row = self._row(entry_id)
        self.assertEqual(row["status"], "delivered")
        self.assertIsNotNone(row["dispatched_at"])
        self.assertEqual(FakeDeliverer.total_delivered(), 1)
        second = drainer.drain_once()
        self.assertEqual(second["claimed"], 0)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(FakeDeliverer.total_delivered(), 1)

    def test_concurrent_drains_do_not_double_deliver(self) -> None:
        self._seed()
        drainer_a = self._drainer()
        drainer_b = self._drainer()
        barrier = threading.Barrier(2)

        def run(drainer: delivery_worker.CloudOutboxDrainer) -> dict:
            barrier.wait()
            return drainer.drain_once()

        threads = [threading.Thread(target=run, args=(d,)) for d in (drainer_a, drainer_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(FakeDeliverer.total_delivered(), 1)
        self.assertEqual(self._row()["status"], "delivered")

    def test_dead_worker_claim_is_reclaimed_after_lease(self) -> None:
        """A row claimed by a worker that died is reclaimed after the lease."""
        entry_id = self._seed()
        worker_a = self._drainer(lease_seconds=60, worker_id="worker-a")
        claimed = worker_a.claim_due(now=1000.0)
        self.assertEqual(len(claimed), 1)
        row = self._row(entry_id)
        self.assertEqual(row["status"], "claimed")
        self.assertEqual(row["claimed_by"], "worker-a")

        # Worker A dies without delivering. A second worker, before the lease
        # expires, cannot claim it.
        worker_b = self._drainer(lease_seconds=60, worker_id="worker-b")
        self.assertEqual(worker_b.claim_due(now=1030.0), [])

        # Lease ownership expires even before another worker performs the
        # reclaim; a late renewal or completion must not extend the old claim.
        self.assertFalse(self.backend.renew_outbox_lease(
            self.tenant_id, entry_id, "worker-a", lease_seconds=60, now=1061.0
        ))
        self.assertFalse(self.backend.mark_outbox_delivered(
            self.tenant_id, entry_id, "worker-a", lease_seconds=60, now=1061.0
        ))
        with LeaseHeartbeat(None, 1.0) as heartbeat:
            self.assertFalse(heartbeat.acquired)
            self.assertTrue(heartbeat.lost.is_set())

        # After the lease expires the claim is reclaimed.
        reclaimed = worker_b.claim_due(now=1100.0)
        self.assertEqual(len(reclaimed), 1)
        row = self._row(entry_id)
        self.assertEqual(row["status"], "claimed")
        self.assertEqual(row["claimed_by"], "worker-b")

        # A late worker must not finalize or reschedule the newer claim.
        self.assertFalse(worker_a.mark_delivered(self.tenant_id, entry_id))
        self.assertFalse(worker_a.mark_retry(self.tenant_id, entry_id, 1, 1200.0, "stale"))
        self.assertFalse(worker_a.mark_dead(self.tenant_id, entry_id, 1, "stale"))
        row = self._row(entry_id)
        self.assertEqual(row["status"], "claimed")
        self.assertEqual(row["claimed_by"], "worker-b")
        self.assertTrue(worker_b.mark_delivered(
            self.tenant_id, entry_id, now=1100.0
        ))
        self.assertEqual(self._row(entry_id)["status"], "delivered")

        # A live but slow delivery must renew its claim instead of being
        # mistaken for a dead worker and delivered a second time.
        slow_entry_id = self._seed(recipient="slow-agent")
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        class SlowDeliverer(delivery_worker.Deliverer):
            def deliver(self, _tenant_id: str, _recipient: str, payload_json: str) -> None:
                calls.append(payload_json)
                started.set()
                if not release.wait(5):
                    raise RuntimeError("slow delivery test timed out")

        live = delivery_worker.CloudOutboxDrainer(
            self.backend, SlowDeliverer(), lease_seconds=0.3, worker_id="live-worker"
        )
        observer = self._drainer(lease_seconds=0.3, worker_id="observer")
        result_holder: list[dict] = []
        thread = threading.Thread(target=lambda: result_holder.append(live.drain_once()))
        thread.start()
        self.assertTrue(started.wait(5), "slow delivery did not start")
        time.sleep(0.8)
        self.assertEqual(observer.claim_due(now=time.time()), [])
        release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "slow delivery worker did not finish")
        self.assertEqual(result_holder[0]["sent"], 1)
        self.assertEqual(self._row(slow_entry_id)["status"], "delivered")
        self.assertEqual(len(calls), 1)

        # Shutdown must not wait forever for a renewal callback that is stuck
        # in a database/network call; the daemon callback is released after
        # the bounded join completes.
        callback_started = threading.Event()
        release_callback = threading.Event()
        callback_calls = 0

        def blocked_renewal() -> bool:
            nonlocal callback_calls
            callback_calls += 1
            if callback_calls == 1:
                return True
            callback_started.set()
            release_callback.wait(5)
            return True

        heartbeat = LeaseHeartbeat(blocked_renewal, 0.03)
        try:
            started_at = time.monotonic()
            with heartbeat:
                self.assertTrue(callback_started.wait(5), "renewal did not start")
            elapsed = time.monotonic() - started_at
        finally:
            release_callback.set()
            thread = heartbeat._thread  # type: ignore[attr-defined]
            if thread is not None:
                thread.join(timeout=2)
        self.assertLess(elapsed, 2.0)

    # -- 2. retry / terminal semantics --------------------------------------

    def test_transient_failure_retried_with_backoff_then_succeeds(self) -> None:
        entry_id = self._seed()
        FakeDeliverer.script = [_Transient]
        drainer = self._drainer(backoff_seconds=60)
        now = time.time()
        first = drainer.drain_once(now=now)
        self.assertEqual(first["retried"], 1)
        self.assertEqual(first["sent"], 0)
        row = self._row(entry_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 1)
        self.assertGreater(row["next_attempt_at"], now)
        self.assertIsNone(row["claimed_by"])

        retry = drainer.drain_once(now=row["next_attempt_at"] + 1)
        self.assertEqual(retry["sent"], 1)
        self.assertEqual(self._row(entry_id)["status"], "delivered")

    def test_permanent_failure_reaches_terminal_dead(self) -> None:
        entry_id = self._seed()
        FakeDeliverer.script = [_Permanent]
        drainer = self._drainer()
        first = drainer.drain_once()
        self.assertEqual(first["dead"], 1)
        row = self._row(entry_id)
        self.assertEqual(row["status"], "dead")
        self.assertGreaterEqual(row["attempts"], 1)
        # A dead row is never retried: the terminal state stops all further
        # attempts (exactly the one attempt that raised was made).
        drainer.drain_once()
        self.assertEqual(FakeDeliverer.total_delivered(), 1)

    def test_max_attempts_on_transient_failures_reaches_dead(self) -> None:
        entry_id = self._seed()
        FakeDeliverer.script = [_Transient, _Transient, _Transient]
        drainer = self._drainer(backoff_seconds=0, max_attempts=2)
        now = time.time()
        first = drainer.drain_once(now=now)
        self.assertEqual(first["retried"], 1)
        row = self._row(entry_id)
        self.assertEqual(row["attempts"], 1)
        second = drainer.drain_once(now=row["next_attempt_at"] + 1)
        self.assertEqual(second["dead"], 1)
        self.assertEqual(self._row(entry_id)["status"], "dead")

    # -- 3. no token or body ever in logs -----------------------------------

    def test_no_log_line_contains_payload(self) -> None:
        secret = "SECRET_PAYLOAD_xyz"
        self._seed(payload=f'{{"seq":1,"text":"{secret}"}}')
        drainer = self._drainer()
        with self.assertLogs(level=logging.DEBUG) as logs:
            drainer.drain_once()
        combined = "\n".join(logs.output)
        self.assertNotIn(secret, combined)


class DeliveryWorkerConfigTests(unittest.TestCase):
    """Drain-worker runtime config + runnable guard."""

    def test_runtime_config_defaults(self) -> None:
        cfg = delivery_worker.runtime_config(argv=[], environ={})
        self.assertEqual(cfg["db_path"], "./data/weft-cloud.db")
        self.assertEqual(cfg["interval"], 5)
        self.assertEqual(cfg["batch_size"], 50)
        self.assertEqual(cfg["max_attempts"], 5)
        self.assertEqual(cfg["lease_seconds"], 60)

    def test_runtime_config_env_overrides(self) -> None:
        cfg = delivery_worker.runtime_config(argv=[], environ={
            "WEFT_DB_PATH": "/data/cloud.db",
            "WEFT_DELIVERY_DRAIN_INTERVAL": "1",
            "WEFT_DELIVERY_DRAIN_BATCH": "10",
            "WEFT_DELIVERY_DRAIN_MAX_ATTEMPTS": "3",
            "WEFT_DELIVERY_DRAIN_BACKOFF": "30",
            "WEFT_DELIVERY_DRAIN_LEASE": "15",
        })
        self.assertEqual(cfg["db_path"], "/data/cloud.db")
        self.assertEqual(cfg["interval"], 1)
        self.assertEqual(cfg["batch_size"], 10)
        self.assertEqual(cfg["max_attempts"], 3)
        self.assertEqual(cfg["backoff_seconds"], 30)
        self.assertEqual(cfg["lease_seconds"], 15)

    def test_runtime_config_invalid_interval_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            delivery_worker.runtime_config(
                argv=[], environ={"WEFT_DELIVERY_DRAIN_INTERVAL": "abc"}
            )
        self.assertIn("WEFT_DELIVERY_DRAIN_INTERVAL", str(ctx.exception))

    def test_worker_without_sink_disables_and_configured_sink_drains_once(self) -> None:
        """The opt-in worker is inert without a sink and idempotent with one."""
        tmp = tempfile.TemporaryDirectory(prefix="cloud-worker-")
        db_path = str(Path(tmp.name) / "cloud.db")
        sink_path = str(Path(tmp.name) / "delivery.jsonl")
        try:
            rc = delivery_worker.main(
                argv=["--once", db_path],
                environ={"WEFT_DELIVERY_SINK": ""},
            )
            self.assertEqual(rc, 0)

            backend = SqliteWalBackend(db_path)
            backend.initialize()
            apply_migrations(backend)
            backend.create_tenant("tenant_worker", "Worker Tenant")
            entry_id = backend.enqueue_outbox(
                "tenant_worker", "env-worker", "agent-dest", '{"seq":1}'
            )
            backend.close()

            rc = delivery_worker.main(
                argv=["--once", db_path],
                environ={"WEFT_DELIVERY_SINK": sink_path},
            )
            self.assertEqual(rc, 0)
            self.assertEqual(
                len(Path(sink_path).read_text(encoding="utf-8").splitlines()),
                1,
            )

            backend = SqliteWalBackend(db_path)
            backend.initialize()
            with backend.transaction() as tx:
                row = tx.execute(
                    "SELECT status FROM cloud_outbox WHERE entry_id = ?", (entry_id,)
                ).fetchone()
            backend.close()
            self.assertEqual(row["status"], "delivered")

            rc = delivery_worker.main(
                argv=["--once", db_path],
                environ={"WEFT_DELIVERY_SINK": sink_path},
            )
            self.assertEqual(rc, 0)
            self.assertEqual(
                len(Path(sink_path).read_text(encoding="utf-8").splitlines()),
                1,
            )
        finally:
            tmp.cleanup()


class CloudOutboxMigrationTests(unittest.TestCase):
    """cloud_010 adds lifecycle columns to an existing cloud_outbox in place."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="cloud-obx-mig-")
        self.cloud_path = self.tmp.name + "/cloud.db"

    def tearDown(self) -> None:
        import gc
        gc.collect()
        self.tmp.cleanup()

    def _apply(self) -> None:
        backend = SqliteWalBackend(self.cloud_path)
        backend.initialize()
        try:
            apply_migrations(backend)
        finally:
            backend.close()

    def _has_column(self, column: str) -> bool:
        import sqlite3
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT 1 FROM pragma_table_info('cloud_outbox') WHERE name = ?",
                (column,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def test_cloud_010_adds_lifecycle_columns_to_existing_outbox(self) -> None:
        from weft_cloud.storage import SqliteWalBackend as Backend

        backend = Backend(self.cloud_path)
        backend.initialize()
        try:
            # Pre-upgrade shape: cloud_outbox WITHOUT lifecycle columns.
            with backend._transaction() as tx:
                tx.execute("DROP TABLE cloud_outbox")
                tx.execute(
                    "CREATE TABLE cloud_outbox ("
                    " entry_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,"
                    " envelope_id TEXT NOT NULL, recipient TEXT NOT NULL,"
                    " payload_json TEXT NOT NULL,"
                    " status TEXT NOT NULL DEFAULT 'queued'"
                    "   CHECK(status IN ('queued','claimed','delivered','dead')),"
                    " attempts INTEGER NOT NULL DEFAULT 0,"
                    " next_attempt_at REAL NOT NULL DEFAULT 0,"
                    " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
            for col in ("claimed_at", "claimed_by", "last_error", "dispatched_at"):
                self.assertFalse(self._has_column(col),
                                 f"precondition: {col} must be absent")
        finally:
            backend.close()

        self._apply()
        for col in ("claimed_at", "claimed_by", "last_error", "dispatched_at"):
            self.assertTrue(self._has_column(col),
                            f"cloud_010 must add {col}")

        import sqlite3
        conn = sqlite3.connect(self.cloud_path)
        try:
            for table, index_name in (
                ("cloud_outbox", "idx_cloud_outbox_claim"),
                ("cloud_identity_outbox", "idx_cloud_identity_outbox_claim"),
            ):
                indexes = {
                    row[1]
                    for row in conn.execute(f"PRAGMA index_list('{table}')").fetchall()
                }
                self.assertIn(index_name, indexes,
                              f"cloud_015 must create {index_name}")
                plan = conn.execute(
                    f"EXPLAIN QUERY PLAN SELECT entry_id FROM {table} "
                    "WHERE (status = ? OR "
                    "       (status = ? AND claimed_at IS NOT NULL AND claimed_at < ?)) "
                    "AND next_attempt_at <= ? ORDER BY created_at LIMIT ?",
                    ("queued", "claimed", 0.0, 0.0, 50),
                ).fetchall()
                details = " | ".join(row[3] for row in plan)
                self.assertIn(index_name, details,
                              f"claim plan for {table} must use {index_name}: {details}")
                self.assertNotIn(f"SCAN {table}", details,
                                 f"claim plan for {table} must not full-scan: {details}")
        finally:
            conn.close()
        self._apply()  # idempotent second run must not raise


if __name__ == "__main__":
    unittest.main()
