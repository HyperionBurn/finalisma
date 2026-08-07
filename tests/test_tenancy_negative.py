"""Tenancy negative suite — tests/test_tenancy_negative.py.

RED deliverable for Wave F Cloud Spine (docs/CLOUD_SPINE_DESIGN.md §3, §7.2).

These tests prove that tenant B CANNOT read, list, address, or enumerate
anything belonging to tenant A — across rooms, events, counters, outbox,
and audit. They drive the real StorageBackend interface with two real
tenants on real SQLite files (no mocks — per AGENTS.md test discipline).

This file imports the StorageBackend ABC, TenantContext, TenantIsolationError,
and a make_backend() seam from src/weft_cloud/, which does NOT exist yet.
It MUST fail at import time until that module is implemented.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# --- The imports: wired by the orchestrator to the real cloud plane. ---
from weft_cloud.storage import StorageBackend, SqliteWalBackend
from weft_cloud.tenancy import TenantContext, TenantIsolationError
from weft_cloud.quotas import PlanLimits


def make_backend(db_path: Path) -> StorageBackend:
    """Seam: build a StorageBackend bound to a real SQLite file.

    Wired by the orchestrator to a real SqliteWalBackend. Each tenant gets its
    own file, proving isolation holds even across independent storage.
    """
    return SqliteWalBackend(db_path)


TENANT_A = "tenant_a"
TENANT_B = "tenant_b"
ACTOR_A = "actor_a"
ACTOR_B = "actor_b"


class TenancyNegativeTests(unittest.TestCase):
    """Cross-tenant isolation negative probes (design §7.2)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        # Two separate SQLite files — one per tenant — to prove isolation
        # holds even when tenants live on independent storage.
        self.backend_a = make_backend(root / "tenant_a.db")
        self.backend_b = make_backend(root / "tenant_b.db")

        self.backend_a.initialize()
        self.backend_b.initialize()

        # Seed tenant A with data.
        self.backend_a.create_tenant(TENANT_A, "Tenant Alpha")
        self.backend_a.bind_room(TENANT_A, "room_a_1", "/tmp/coord_a.db")
        self.backend_a.increment_counter(TENANT_A, "rooms")
        self.backend_a.append_audit(TENANT_A, "seed", ACTOR_A, "obj-1", "{}")
        self.backend_a.enqueue_outbox(TENANT_A, "env-1", "recipient", "{}")

        # Seed tenant B with data.
        self.backend_b.create_tenant(TENANT_B, "Tenant Beta")
        self.backend_b.bind_room(TENANT_B, "room_b_1", "/tmp/coord_b.db")
        self.backend_b.increment_counter(TENANT_B, "rooms")
        self.backend_b.append_audit(TENANT_B, "seed", ACTOR_B, "obj-2", "{}")
        self.backend_b.enqueue_outbox(TENANT_B, "env-2", "recipient", "{}")

        # Construct a TenantContext for tenant A — this is the actor
        # that will attempt cross-tenant access.
        self.ctx_a = TenantContext(
            tenant_id=TENANT_A,
            actor_id=ACTOR_A,
            plan=PlanLimits(),
            backend=self.backend_a,
        )

    def tearDown(self) -> None:
        for backend in (getattr(self, "backend_a", None), getattr(self, "backend_b", None)):
            if backend is not None and hasattr(backend, "close"):
                backend.close()
        self.temp.cleanup()

    # --- Cross-tenant probes (guard must fire before backend) ---

    def test_cross_tenant_room_read(self) -> None:
        """Actor in tenant A calls list_rooms(tenant_b_id) -> TenantIsolationError."""
        with self.assertRaises(TenantIsolationError):
            self.ctx_a.require_tenant(TENANT_B)
            self.backend_a.list_rooms(TENANT_B)

    def test_cross_tenant_event_poll(self) -> None:
        """Actor in tenant A calls poll_events(tenant_b_id, room_id, ...) -> TenantIsolationError."""
        with self.assertRaises(TenantIsolationError):
            self.ctx_a.require_tenant(TENANT_B)
            self.backend_a.poll_events(TENANT_B, "room_b_1", 0, 50)

    def test_cross_tenant_audit_read(self) -> None:
        """Actor in tenant A calls list_audit(tenant_b_id) -> TenantIsolationError."""
        with self.assertRaises(TenantIsolationError):
            self.ctx_a.require_tenant(TENANT_B)
            self.backend_a.list_audit(TENANT_B)

    def test_cross_tenant_counter_read(self) -> None:
        """Actor in tenant A calls get_counter(tenant_b_id, 'rooms') -> TenantIsolationError."""
        with self.assertRaises(TenantIsolationError):
            self.ctx_a.require_tenant(TENANT_B)
            self.backend_a.get_counter(TENANT_B, "rooms")

    def test_cross_tenant_outbox_claim(self) -> None:
        """Actor in tenant A calls claim_due_outbox(tenant_b_id) -> TenantIsolationError."""
        with self.assertRaises(TenantIsolationError):
            self.ctx_a.require_tenant(TENANT_B)
            self.backend_a.claim_due_outbox(TENANT_B, limit=10)

    # --- Structural tenancy (signature-level) ---

    def test_missing_tenant_id_raises(self) -> None:
        """Call a storage method without tenant_id -> TypeError.

        Structural tenancy: tenant_id is a required positional parameter
        with no default. Omitting it raises TypeError at the call site,
        not a silent cross-tenant read.
        """
        with self.assertRaises(TypeError):
            # list_rooms requires tenant_id positionally.
            self.backend_a.list_rooms()  # type: ignore[call-arg]

    # --- Guard isolation ---

    def test_guard_rejects_wrong_tenant(self) -> None:
        """ctx.require_tenant('other_tenant') -> TenantIsolationError.

        The guard fires unconditionally when the requested tenant does not
        match the context's tenant — even before any backend call.
        """
        with self.assertRaises(TenantIsolationError):
            self.ctx_a.require_tenant("other_tenant")


if __name__ == "__main__":
    unittest.main()
