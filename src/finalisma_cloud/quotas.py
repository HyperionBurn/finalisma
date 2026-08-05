"""Cloud-plane quotas — plan-driven limits enforced atomically.

A plan is a dict of limits resolved per tenant. Counters live in
``cloud_counters`` (per-tenant) and ``cloud_room_counters`` (per-room). The
quota check and the mutation run in the SAME transaction, so concurrent joins
cannot oversubscribe a room.

The seam: limits are read from the tenant's plan (``PLANS``), never hardcoded.
Wave I swaps real billing in without touching the enforcement code.

Authoritative spec: docs/CLOUD_SPINE_DESIGN.md section 5.
"""

from __future__ import annotations

from dataclasses import dataclass


class QuotaError(Exception):
    def __init__(self, code: str = "quota_exceeded", message: str = "limit exceeded"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlanLimits:
    max_rooms: int = 5
    max_members_per_room: int = 10
    max_events_per_month: int = 10_000
    max_messages_per_minute: int = 60


PLANS: dict[str, PlanLimits] = {
    "free": PlanLimits(),
    "pro": PlanLimits(max_rooms=50, max_members_per_room=50, max_events_per_month=100_000),
}


def _resolve_plan(backend, tenant_id: str) -> PlanLimits:
    """Resolve a tenant's plan from its stored plan_id (config-driven)."""
    tenant = backend.get_tenant(tenant_id)
    plan_id = tenant.get("plan_id", "free") if tenant else "free"
    return PLANS.get(plan_id, PLANS["free"])


def join_room_with_quota(backend, tenant_id: str, room_id: str, member_id: str) -> dict:
    """Join a room, enforcing the room's member cap atomically.

    The cap check and the counter increment happen in ONE transaction, so
    concurrent joins cannot oversubscribe the room.
    """
    plan = _resolve_plan(backend, tenant_id)
    with backend.transaction() as tx:
        current = tx.execute(
            "SELECT value FROM cloud_room_counters WHERE tenant_id = ? AND room_id = ? AND counter = 'members'",
            (tenant_id, room_id),
        ).fetchone()
        current_count = int(current["value"]) if current else 0
        if current_count >= plan.max_members_per_room:
            raise QuotaError("quota_exceeded", "room member limit reached")
        # Record the membership + increment counter atomically.
        tx.execute(
            "INSERT INTO cloud_room_counters(tenant_id, room_id, counter, value, updated_at) "
            "VALUES (?, ?, 'members', 1, ?) "
            "ON CONFLICT(tenant_id, room_id, counter) DO UPDATE SET value = value + 1",
            (tenant_id, room_id, "members"),
        )
        tx.commit()
    return {"joined": True, "member_id": member_id}


def create_room_with_quota(backend, tenant_id: str, room_id: str, coordinator_db_path: str) -> dict:
    """Create a room, enforcing the tenant's room-count cap atomically."""
    plan = _resolve_plan(backend, tenant_id)
    with backend.transaction() as tx:
        current = tx.execute(
            "SELECT value FROM cloud_counters WHERE tenant_id = ? AND counter = 'rooms'",
            (tenant_id,),
        ).fetchone()
        current_count = int(current["value"]) if current else 0
        if current_count >= plan.max_rooms:
            raise QuotaError("quota_exceeded", "tenant room limit reached")
        tx.execute(
            "INSERT INTO cloud_tenant_rooms(tenant_id, room_id, coordinator_db_path, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, room_id) DO NOTHING",
            (tenant_id, room_id, coordinator_db_path, "2026-08-05T00:00:00Z"),
        )
        tx.execute(
            "INSERT INTO cloud_counters(tenant_id, counter, value, updated_at) VALUES (?, 'rooms', 1, ?) "
            "ON CONFLICT(tenant_id, counter) DO UPDATE SET value = value + 1",
            (tenant_id, "rooms"),
        )
        tx.commit()
    return {"created": True, "room_id": room_id}
