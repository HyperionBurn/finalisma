"""Quota + rate-limit refusal tests for the Wave F Cloud Spine.

This is the RED deliverable for design §7.4. Tests exercise REAL storage
(no mocks) through the `make_backend()` factory seam — the single seam the
orchestrator fills in. Every test imports the cloud quota/rate-limit entry
points (weft_cloud.quotas, weft_cloud.rate_limit) which do not
exist yet, so the file fails until the implementation is wired in.

Per design §5:
  - PlanLimits is a frozen dataclass of per-plan limits.
  - PLANS is a dict[str, PlanLimits] (free/pro, config-driven seam).
  - Quota enforcement raises QuotaError with code "quota_exceeded".
  - RateLimiter.check() returns RateResult(allowed, remaining, retry_after).
  - Rate refusal carries code "rate_limited" + a Retry-After value.
  - Quota check + mutation are atomic (same transaction).
"""

from __future__ import annotations

import threading
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# These modules now exist — wired by the orchestrator to the real cloud plane.
from weft_cloud.storage import StorageBackend, SqliteWalBackend
from weft_cloud.quotas import PlanLimits, PLANS, QuotaError, join_room_with_quota, create_room_with_quota
from weft_cloud.rate_limit import RateLimiter, RateResult, RateLimitedError


def make_backend() -> StorageBackend:
    """Factory seam — wired to a real SqliteWalBackend on a fresh temp file.

    No test imports a concrete backend directly; they go through this factory,
    so the same suite passes unchanged for a future PostgresBackend.
    """
    return SqliteWalBackend(tempfile.mkstemp(suffix=".db")[1])


class QuotaRateLimitRefusalTests(unittest.TestCase):
    """Design §7.4 — quota + rate-limit refusal against real storage."""

    def setUp(self) -> None:
        self.backend = make_backend()
        self.backend.initialize()

    def tearDown(self) -> None:
        if hasattr(self.backend, "close"):
            self.backend.close()

    # --- 1. test_room_member_cap ---
    def test_room_member_cap(self) -> None:
        """Join members until the plan cap, attempt one more -> quota_exceeded.

        Uses the free plan (max_members_per_room=15). Creates a tenant +
        room, joins `cap` members successfully, then the (cap+1)th join must
        raise QuotaError with code "quota_exceeded".
        """
        plan = PLANS["free"]
        cap = plan.max_members_per_room
        self.assertGreater(cap, 0, "free plan must have a positive member cap")

        self.backend.create_tenant("tenant_cap", "Tenant Cap", plan_id="free")
        self.backend.bind_room("tenant_cap", "room-1", "/tmp/coord-cap.db")

        # Join `cap` members — all must succeed.
        for i in range(cap):
            result = join_room_with_quota(
                self.backend, "tenant_cap", "room-1", f"member-{i}"
            )
            self.assertTrue(result["joined"], f"member-{i} should join under cap")

        # The counter must now sit exactly at the cap.
        current = self.backend.get_room_counter("tenant_cap", "room-1", "members")
        self.assertEqual(current, cap)

        # One more -> quota_exceeded.
        with self.assertRaises(QuotaError) as ctx:
            join_room_with_quota(
                self.backend, "tenant_cap", "room-1", "member-over-cap"
            )
        self.assertEqual(ctx.exception.code, "quota_exceeded")

        # The over-cap join must NOT have been recorded.
        self.assertEqual(
            self.backend.get_room_counter("tenant_cap", "room-1", "members"), cap
        )

    # --- 2. test_tenant_room_cap ---
    def test_tenant_room_cap(self) -> None:
        """Create rooms until the plan cap, attempt one more -> quota_exceeded.

        Uses the free plan (max_rooms=5). Creates `cap` rooms successfully,
        then the (cap+1)th creation must raise QuotaError "quota_exceeded".
        """
        plan = PLANS["free"]
        cap = plan.max_rooms
        self.assertGreater(cap, 0, "free plan must have a positive room cap")

        self.backend.create_tenant("tenant_rc", "Tenant Room Cap", plan_id="free")

        # Create `cap` rooms — all must succeed.
        for i in range(cap):
            result = create_room_with_quota(
                self.backend, "tenant_rc", f"room-{i}", f"/tmp/coord-r{i}.db"
            )
            self.assertTrue(result["created"], f"room-{i} should be created under cap")

        # The tenant's room counter must sit exactly at the cap.
        self.assertEqual(self.backend.get_counter("tenant_rc", "rooms"), cap)

        # One more -> quota_exceeded.
        with self.assertRaises(QuotaError) as ctx:
            create_room_with_quota(
                self.backend, "tenant_rc", "room-over-cap", "/tmp/coord-over.db"
            )
        self.assertEqual(ctx.exception.code, "quota_exceeded")

        # The over-cap room must NOT have been recorded.
        self.assertEqual(self.backend.get_counter("tenant_rc", "rooms"), cap)

    # --- 3. test_rate_limit_exhaustion ---
    def test_rate_limit_exhaustion(self) -> None:
        """Exhaust the rate limit in a window -> rate_limited + Retry-After.

        Uses the free plan's max_messages_per_minute as the limit. Issues
        requests until one is refused; the refusal must carry code
        "rate_limited" and a positive Retry-After value.
        """
        plan = PLANS["free"]
        limit = plan.max_messages_per_minute
        self.assertGreater(limit, 0, "free plan must have a positive rate limit")

        self.backend.create_tenant("tenant_rl", "Tenant Rate Limit", plan_id="free")
        limiter = RateLimiter()

        # Issue `limit` requests — all allowed.
        for i in range(limit):
            result = limiter.check(
                self.backend, "tenant_rl", None, "messages",
                max_allowed=limit, window_seconds=60,
            )
            self.assertTrue(result.allowed, f"request {i} should be allowed under limit")
            self.assertIsNone(result.retry_after, "allowed request has no Retry-After")

        # The next request is refused.
        result = limiter.check(
            self.backend, "tenant_rl", None, "messages",
            max_allowed=limit, window_seconds=60,
        )
        self.assertFalse(result.allowed)
        self.assertIsNotNone(result.retry_after, "refused request must carry Retry-After")
        self.assertGreater(result.retry_after, 0.0, "Retry-After must be positive")

        # The refusal surfaces as a RateLimitedError with code "rate_limited"
        # when the enforcement helper is used.
        with self.assertRaises(RateLimitedError) as ctx:
            limiter.enforce(
                self.backend, "tenant_rl", None, "messages",
                max_allowed=limit, window_seconds=60,
            )
        self.assertEqual(ctx.exception.code, "rate_limited")
        self.assertIsNotNone(ctx.exception.retry_after)
        self.assertGreater(ctx.exception.retry_after, 0.0)

    # --- 4. test_atomic_quota_check ---
    def test_atomic_quota_check(self) -> None:
        """Concurrent joins at the cap boundary -> exactly cap members,
        no over-subscription.

        The quota check + mutation MUST be atomic. If they were not, two
        concurrent joins could both read "current < cap" and both insert,
        oversubscribing the room. This test fires (cap + N) concurrent
        join attempts at an empty room and asserts that exactly `cap`
        succeed and the counter never exceeds the cap.
        """
        plan = PLANS["free"]
        cap = plan.max_members_per_room
        self.assertGreater(cap, 0)

        self.backend.create_tenant("tenant_atom", "Tenant Atomic", plan_id="free")
        self.backend.bind_room("tenant_atom", "room-atom", "/tmp/coord-atom.db")

        # Fire (cap + 20) concurrent join attempts at an empty room.
        overshoot = 20
        total_attempts = cap + overshoot
        barrier = threading.Barrier(total_attempts)
        results: list[bool] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def attempt(member_id: str) -> None:
            barrier.wait()
            try:
                result = join_room_with_quota(
                    self.backend, "tenant_atom", "room-atom", member_id
                )
                with lock:
                    results.append(result["joined"])
            except QuotaError:
                with lock:
                    errors.append(QuotaError("quota_exceeded", "ok"))
            except Exception as e:  # noqa: BLE001 — surface unexpected failures
                with lock:
                    errors.append(e)

        threads = [
            threading.Thread(target=attempt, args=(f"m-{i}",))
            for i in range(total_attempts)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # No unexpected exceptions.
        self.assertEqual(
            [type(e).__name__ for e in errors],
            ["QuotaError"] * len(errors),
            "only QuotaError may be raised under contention",
        )

        # Exactly `cap` joins succeeded.
        self.assertEqual(
            sum(1 for joined in results if joined),
            cap,
            f"exactly {cap} joins must succeed, no over-subscription",
        )

        # The rest were refused as quota_exceeded.
        self.assertEqual(len(errors), overshoot)

        # The counter is exactly the cap — never oversubscribed.
        final = self.backend.get_room_counter("tenant_atom", "room-atom", "members")
        self.assertEqual(final, cap)
        self.assertLessEqual(final, cap)

    # --- 5. test_plan_seam ---
    def test_plan_seam(self) -> None:
        """Two tenants with different plans (free vs pro) resolve different
        PlanLimits from the same enforcement code — proves the seam is
        config-driven, not hardcoded.

        A free tenant must be refused at the free cap. A pro tenant, in a
        separate room, must be allowed well past the free cap (up to the pro
        cap) using the IDENTICAL enforcement function. The only difference is
        the plan resolved for each tenant.
        """
        free_plan = PLANS["free"]
        pro_plan = PLANS["pro"]
        self.assertEqual(free_plan.max_members_per_room, 15)
        self.assertGreater(
            pro_plan.max_members_per_room,
            free_plan.max_members_per_room,
            "pro plan must exceed free plan for the seam test to be meaningful",
        )

        self.backend.create_tenant("tenant_free", "Free Tenant", plan_id="free")
        self.backend.create_tenant("tenant_pro", "Pro Tenant", plan_id="pro")
        self.backend.bind_room("tenant_free", "room-free", "/tmp/coord-free.db")
        self.backend.bind_room("tenant_pro", "room-pro", "/tmp/coord-pro.db")

        free_cap = free_plan.max_members_per_room
        pro_cap = pro_plan.max_members_per_room

        # Free tenant: joins up to free_cap succeed, next is refused.
        for i in range(free_cap):
            join_room_with_quota(
                self.backend, "tenant_free", "room-free", f"free-m-{i}"
            )
        with self.assertRaises(QuotaError) as ctx_free:
            join_room_with_quota(
                self.backend, "tenant_free", "room-free", "free-m-over"
            )
        self.assertEqual(ctx_free.exception.code, "quota_exceeded")

        # Pro tenant: the SAME enforcement function allows past the free cap,
        # all the way to the pro cap. This proves the limit comes from the
        # plan, not from a hardcoded constant in the enforcement code.
        for i in range(free_cap + 5):
            result = join_room_with_quota(
                self.backend, "tenant_pro", "room-pro", f"pro-m-{i}"
            )
            self.assertTrue(
                result["joined"],
                f"pro tenant join {i} should succeed past free cap ({free_cap})",
            )

        # Pro tenant counter is past the free cap.
        pro_count = self.backend.get_room_counter("tenant_pro", "room-pro", "members")
        self.assertEqual(pro_count, free_cap + 5)
        self.assertGreater(pro_count, free_cap)

        # Free tenant counter is exactly at its cap — no cross-tenant bleed.
        free_count = self.backend.get_room_counter("tenant_free", "room-free", "members")
        self.assertEqual(free_count, free_cap)

        # The pro tenant is refused only at the pro cap.
        for i in range(free_cap + 5, pro_cap):
            join_room_with_quota(
                self.backend, "tenant_pro", "room-pro", f"pro-m-{i}"
            )
        with self.assertRaises(QuotaError) as ctx_pro:
            join_room_with_quota(
                self.backend, "tenant_pro", "room-pro", "pro-m-over"
            )
        self.assertEqual(ctx_pro.exception.code, "quota_exceeded")
        self.assertEqual(
            self.backend.get_room_counter("tenant_pro", "room-pro", "members"), pro_cap
        )


if __name__ == "__main__":
    unittest.main()
