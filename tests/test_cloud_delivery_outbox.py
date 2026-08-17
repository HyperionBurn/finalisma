"""Hosted delivery outbox lifecycle — the cloud_outbox completion state machine.

RED deliverable: ``cloud_outbox`` today supports only enqueue and
``queued -> claimed``; a claimed row is stuck forever if its worker dies.
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
from weft_cloud.storage import SqliteWalBackend

# RED gate: the module does not exist yet.
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

        # After the lease expires the claim is reclaimed.
        reclaimed = worker_b.claim_due(now=1100.0)
        self.assertEqual(len(reclaimed), 1)
        row = self._row(entry_id)
        self.assertEqual(row["status"], "claimed")
        self.assertEqual(row["claimed_by"], "worker-b")

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

    def test_worker_without_deliverer_config_exits_cleanly(self) -> None:
        """No sink configured -> the worker exits cleanly and drains nothing."""
        tmp = tempfile.TemporaryDirectory(prefix="cloud-worker-")
        db_path = str(Path(tmp.name) / "cloud.db")
        try:
            rc = delivery_worker.main(
                argv=["--once", db_path],
                environ={"WEFT_DELIVERY_SINK": ""},
            )
            self.assertEqual(rc, 0)
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
