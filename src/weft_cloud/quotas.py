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

import datetime as _dt
from dataclasses import dataclass

from weft_cloud.storage import utc_now_iso


class QuotaError(Exception):
    """Plan limit hit. Carries structured, non-leaking detail for the HTTP surface.

    ``limit_name`` / ``limit_value`` / ``plan_id`` describe the caller's OWN
    plan and limit — never another tenant's data and never an internal id.
    """

    def __init__(self, code: str = "quota_exceeded", message: str = "limit exceeded",
                 *, limit_name: str | None = None, limit_value: int | None = None,
                 plan_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.limit_name = limit_name
        self.limit_value = limit_value
        self.plan_id = plan_id


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


def resolve_plan(backend, tenant_id: str) -> tuple[str, PlanLimits]:
    """Resolve a tenant's ``(plan_id, PlanLimits)`` from its stored plan_id.

    ``PLANS`` is the single source of truth for limit values. Unknown or
    absent plan_ids fall back to ``free`` (the storage default).
    """
    tenant = backend.get_tenant(tenant_id)
    plan_id = tenant.get("plan_id", "free") if tenant else "free"
    return plan_id, PLANS.get(plan_id, PLANS["free"])


def plan_limits(backend, tenant_id: str) -> PlanLimits:
    """Resolve just the PlanLimits for a tenant (drop-in for callers that do
    not need the plan id)."""
    _, limits = resolve_plan(backend, tenant_id)
    return limits


def validate_room_cap(backend, tenant_id: str, cap: int) -> None:
    """Reject a requested room ``cap`` above the tenant's plan member limit.

    Rejecting (not silently clamping) keeps the response honest: the caller
    asked for X and is told it is above their plan, instead of quietly being
    given less than they asked for.
    """
    plan_id, plan = resolve_plan(backend, tenant_id)
    if int(cap) > plan.max_members_per_room:
        raise QuotaError(
            "quota_exceeded",
            f"cap exceeds the {plan_id} plan's room member limit "
            f"(max {plan.max_members_per_room} members per room)",
            limit_name="max_members_per_room",
            limit_value=plan.max_members_per_room,
            plan_id=plan_id,
        )


def join_room_with_quota(backend, tenant_id: str, room_id: str, member_id: str) -> dict:
    """Join a room, enforcing the room's member cap atomically.

    The cap check and the counter increment happen in ONE transaction, so
    concurrent joins cannot oversubscribe the room.

    Standalone seam (also used for the owner auto-join on room create). The
    HTTP join path in ``CloudRoomService.join_room`` runs the same gate
    INSIDE its own transaction so the increment commits or rolls back with the
    membership insert.
    """
    plan_id, plan = resolve_plan(backend, tenant_id)
    with backend.transaction() as tx:
        increment_room_member_counter(tx, tenant_id, room_id, plan_id, plan)
        tx.commit()
    return {"joined": True, "member_id": member_id}


def increment_room_member_counter(tx, tenant_id: str, room_id: str, plan_id: str,
                                  plan: PlanLimits) -> None:
    """Plan-member-cap check + atomic counter increment on an OPEN transaction.

    ``tx`` is the caller's ``StorageTransaction``. The check and the increment
    run on the caller's transaction, so the increment commits or rolls back
    together with the membership write that surrounds it — a join refused later
    in the same transaction (e.g. ``room_full``) leaves no trace on the counter.

    Raises ``QuotaError`` (code ``quota_exceeded``) when the plan's per-room
    member cap is already reached; the counter is left untouched.
    """
    current = tx.execute(
        "SELECT value FROM cloud_room_counters WHERE tenant_id = ? AND room_id = ? AND counter = 'members'",
        (tenant_id, room_id),
    ).fetchone()
    current_count = int(current["value"]) if current else 0
    if current_count >= plan.max_members_per_room:
        raise QuotaError(
            "quota_exceeded",
            f"room member limit reached (max {plan.max_members_per_room} "
            f"members per room on the {plan_id} plan)",
            limit_name="max_members_per_room",
            limit_value=plan.max_members_per_room,
            plan_id=plan_id,
        )
    tx.execute(
        "INSERT INTO cloud_room_counters(tenant_id, room_id, counter, value, updated_at) "
        "VALUES (?, ?, 'members', 1, ?) "
        "ON CONFLICT(tenant_id, room_id, counter) DO UPDATE SET value = value + 1, "
        "updated_at = excluded.updated_at",
        (tenant_id, room_id, utc_now_iso()),
    )


def bind_room_with_quota_in_tx(tx, tenant_id: str, room_id: str,
                               coordinator_db_path: str, plan_id: str,
                               plan: PlanLimits) -> None:
    """Tenant-room cap check + binding + counter increment on an OPEN transaction."""
    current = tx.execute(
        "SELECT value FROM cloud_counters WHERE tenant_id = ? AND counter = 'rooms'",
        (tenant_id,),
    ).fetchone()
    current_count = int(current["value"]) if current else 0
    if current_count >= plan.max_rooms:
        raise QuotaError(
            "quota_exceeded",
            f"tenant room limit reached (max {plan.max_rooms} rooms "
            f"on the {plan_id} plan)",
            limit_name="max_rooms",
            limit_value=plan.max_rooms,
            plan_id=plan_id,
        )
    tx.execute(
        "INSERT INTO cloud_tenant_rooms(tenant_id, room_id, coordinator_db_path, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, room_id) DO NOTHING",
        (tenant_id, room_id, coordinator_db_path, utc_now_iso()),
    )
    tx.execute(
        "INSERT INTO cloud_counters(tenant_id, counter, value, updated_at) VALUES (?, 'rooms', 1, ?) "
        "ON CONFLICT(tenant_id, counter) DO UPDATE SET value = value + 1, "
        "updated_at = excluded.updated_at",
        (tenant_id, utc_now_iso()),
    )


def events_month_bucket() -> str:
    """Counter key for the current calendar month (UTC), e.g. ``events:2026-08``.

    A new bucket per month is what makes ``max_events_per_month`` a MONTHLY
    budget instead of a lifetime one.
    """
    return f"events:{_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m')}"


def enforce_events_per_month(tx, tenant_id: str, plan_id: str, plan: PlanLimits) -> None:
    """Enforce the plan's monthly event budget atomically on an OPEN transaction.

    Reads the tenant's ``events:<YYYY-MM>`` counter, refuses with ``QuotaError``
    (code ``quota_exceeded``) once the month's budget is spent, then increments
    the bucket. Runs on the caller's transaction so the counter and the event
    it accounts for commit or roll back together.
    """
    bucket = events_month_bucket()
    current = tx.execute(
        "SELECT value FROM cloud_counters WHERE tenant_id = ? AND counter = ?",
        (tenant_id, bucket),
    ).fetchone()
    current_count = int(current["value"]) if current else 0
    if current_count >= plan.max_events_per_month:
        raise QuotaError(
            "quota_exceeded",
            f"monthly event limit reached (max {plan.max_events_per_month} "
            f"events per month on the {plan_id} plan)",
            limit_name="max_events_per_month",
            limit_value=plan.max_events_per_month,
            plan_id=plan_id,
        )
    tx.execute(
        "INSERT INTO cloud_counters(tenant_id, counter, value, updated_at) VALUES (?, ?, 1, ?) "
        "ON CONFLICT(tenant_id, counter) DO UPDATE SET value = value + 1, "
        "updated_at = excluded.updated_at",
        (tenant_id, bucket, utc_now_iso()),
    )


def create_room_with_quota(backend, tenant_id: str, room_id: str, coordinator_db_path: str) -> dict:
    """Create a room binding, enforcing the tenant's room-count cap atomically."""
    plan_id, plan = resolve_plan(backend, tenant_id)
    with backend.transaction() as tx:
        bind_room_with_quota_in_tx(tx, tenant_id, room_id, coordinator_db_path, plan_id, plan)
        tx.commit()
    return {"created": True, "room_id": room_id}
