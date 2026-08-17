"""Storage conformance suite for the Wave F Cloud Spine.

This is the RED deliverable: tests run against the StorageBackend ABC only,
through a `make_backend()` factory. The factory is the single seam the
orchestrator fills in — it raises NotImplementedError here so the suite fails
until SqliteWalBackend is wired in.

Per design §7.1, every test exercises REAL storage (no mocks). The same suite
must pass unchanged for a future PostgresBackend.
"""

from __future__ import annotations

import threading
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Import the ABC and the concrete backend (the orchestrator wires the seam).
from weft_cloud.storage import StorageBackend, SqliteWalBackend


def make_backend() -> StorageBackend:
    """Factory seam — wired by the orchestrator to a real SqliteWalBackend.

    Each call returns a fresh backend on a unique temp SQLite file so tests
    are isolated. A future PostgresBackend swaps in here; the suite is unchanged.
    """
    return SqliteWalBackend(tempfile.mkstemp(suffix=".db")[1])


class StorageConformanceTests(unittest.TestCase):
    """Run against ANY backend. The same tests pass for SqliteWalBackend
    and (later) PostgresBackend unchanged."""

    def setUp(self) -> None:
        self.backend = make_backend()
        self.backend.initialize()

    def tearDown(self) -> None:
        if hasattr(self.backend, "close"):
            self.backend.close()

    # --- 1. tenant_scoping ---
    def test_tenant_scoping(self) -> None:
        """create rooms for tenant_a and tenant_b; list_rooms(tenant_a)
        returns only tenant_a's rooms."""
        self.backend.create_tenant("tenant_a", "Tenant A")
        self.backend.create_tenant("tenant_b", "Tenant B")
        self.backend.bind_room("tenant_a", "room-a1", "/tmp/coord-a1.db")
        self.backend.bind_room("tenant_a", "room-a2", "/tmp/coord-a2.db")
        self.backend.bind_room("tenant_b", "room-b1", "/tmp/coord-b1.db")

        rooms_a = self.backend.list_rooms("tenant_a")
        rooms_b = self.backend.list_rooms("tenant_b")

        room_a_ids = {r["room_id"] for r in rooms_a}
        room_b_ids = {r["room_id"] for r in rooms_b}

        self.assertEqual(room_a_ids, {"room-a1", "room-a2"})
        self.assertEqual(room_b_ids, {"room-b1"})
        # Cross-tenant leak check: tenant_a must not see tenant_b's rooms.
        self.assertNotIn("room-b1", room_a_ids)
        self.assertNotIn("room-a1", room_b_ids)

    # --- 2. transaction_rollback ---
    def test_transaction_rollback(self) -> None:
        """start a transaction, insert a room, raise, rollback;
        the room is absent on re-read."""
        self.backend.create_tenant("tenant_r", "Tenant R")

        try:
            with self.backend.transaction() as tx:
                tx.execute(
                    "INSERT INTO cloud_tenant_rooms (tenant_id, room_id, coordinator_db_path, created_at) VALUES (?, ?, ?, ?)",
                    ("tenant_r", "rolled-back-room", "/tmp/rolled-back.db", "2026-08-05T00:00:00Z"),
                )
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

        # After rollback, the room must not exist.
        rooms = self.backend.list_rooms("tenant_r")
        self.assertEqual(rooms, [])

        # A committed room in the same backend must persist — proving the
        # rollback was real and not a global wipe.
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_tenant_rooms (tenant_id, room_id, coordinator_db_path, created_at) VALUES (?, ?, ?, ?)",
                ("tenant_r", "committed-room", "/tmp/committed.db", "2026-08-05T00:00:01Z"),
            )
            tx.commit()
        rooms = self.backend.list_rooms("tenant_r")
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["room_id"], "committed-room")

    # --- 3. counter_atomicity ---
    def test_counter_atomicity(self) -> None:
        """two threads increment the same counter 100× each; final value = 200."""
        self.backend.create_tenant("tenant_c", "Tenant C")
        # Seed the counter at 0.
        self.backend.increment_counter("tenant_c", "shared", 0)

        iterations = 100
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            for _ in range(iterations):
                self.backend.increment_counter("tenant_c", "shared", 1)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        self.assertEqual(self.backend.get_counter("tenant_c", "shared"), 2 * iterations)

    # --- 4. rate_limit_window ---
    def test_rate_limit_window(self) -> None:
        """check_rate_limit 70× with max_allowed=60 in a window;
        first 60 allowed, next 10 refused."""
        self.backend.create_tenant("tenant_l", "Tenant L")
        allowed = 0
        refused = 0
        for _ in range(70):
            ok, _remaining = self.backend.check_rate_limit(
                "tenant_l", None, "api_calls", max_allowed=60, window_seconds=60
            )
            if ok:
                allowed += 1
            else:
                refused += 1

        self.assertEqual(allowed, 60)
        self.assertEqual(refused, 10)

    # --- 5. audit_append_read ---
    def test_audit_append_read(self) -> None:
        """append_audit then list_audit; entry present with correct action/actor."""
        self.backend.create_tenant("tenant_audit", "Tenant Audit")
        self.backend.append_audit(
            "tenant_audit",
            action="room.create",
            actor="agent-x",
            object_id="room-1",
            payload='{"source":"test"}',
        )

        entries = self.backend.list_audit("tenant_audit")
        self.assertGreaterEqual(len(entries), 1)
        latest = entries[0]
        self.assertEqual(latest["action"], "room.create")
        self.assertEqual(latest["actor"], "agent-x")
        self.assertEqual(latest["object_id"], "room-1")

    # --- 6. migration_idempotency ---
    def test_migration_idempotency(self) -> None:
        """apply_migration twice; second run no-op, version unchanged."""
        self.backend.create_tenant("tenant_mig", "Tenant Mig")

        # Apply a real migration via the interface's apply_migration method.
        self.backend.apply_migration(
            "cloud_001_init",
            "CREATE TABLE IF NOT EXISTS cloud_tenants (tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL)",
        )
        version_after_first = self.backend.get_schema_version()

        # Second run of the same migration must be a no-op (idempotent).
        self.backend.apply_migration(
            "cloud_001_init",
            "CREATE TABLE IF NOT EXISTS cloud_tenants (tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL)",
        )
        version_after_second = self.backend.get_schema_version()

        self.assertEqual(version_after_first, version_after_second)
        self.assertGreaterEqual(version_after_first, 1)

    # --- 7. outbox_enqueue_claim ---
    def test_outbox_enqueue_claim(self) -> None:
        """enqueue_outbox then claim_due_outbox; entry claimable, status transitions."""
        self.backend.create_tenant("tenant_o", "Tenant O")
        entry_id = self.backend.enqueue_outbox(
            "tenant_o", "env-1", "agent-dest", '{"msg":"hello"}'
        )
        self.assertIsInstance(entry_id, str)
        self.assertTrue(entry_id)

        due = self.backend.claim_due_outbox("tenant_o", limit=10)
        self.assertGreaterEqual(len(due), 1)
        claimed = due[0]
        self.assertEqual(claimed["envelope_id"], "env-1")
        self.assertEqual(claimed["recipient"], "agent-dest")
        # Status must have transitioned from 'queued' to 'claimed'.
        self.assertEqual(claimed["status"], "claimed")


if __name__ == "__main__":
    unittest.main()
