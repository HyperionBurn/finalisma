"""Cloud-plane rate limiting — a configurable seam.

v1 uses ``cloud_rate_windows`` rows (SQLite) with windowed cleanup. Wave I can
swap in a Redis-backed limiter without touching enforcement code, because the
enforcement calls ``limiter.check()`` / ``limiter.enforce()`` only.

Authoritative spec: docs/CLOUD_SPINE_DESIGN.md section 5.3.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


class RateLimitedError(Exception):
    def __init__(self, message: str = "rate limit exceeded", retry_after: float = 0.0):
        super().__init__(message)
        self.code = "rate_limited"
        self.retry_after = retry_after


@dataclass
class RateResult:
    allowed: bool
    remaining: int
    retry_after: float | None


class RateLimiter:
    """Sliding-window rate limiter over the storage backend."""

    def check(self, backend: Any, tenant_id: str, room_id: str | None, key: str,
              max_allowed: int, window_seconds: int) -> RateResult:
        allowed, remaining = backend.check_rate_limit(
            tenant_id, room_id, key, max_allowed=max_allowed, window_seconds=window_seconds
        )
        if allowed:
            return RateResult(allowed=True, remaining=remaining, retry_after=None)
        return RateResult(allowed=False, remaining=remaining, retry_after=float(window_seconds))

    def enforce(self, backend: Any, tenant_id: str, room_id: str | None, key: str,
                max_allowed: int, window_seconds: int) -> None:
        result = self.check(backend, tenant_id, room_id, key, max_allowed, window_seconds)
        if not result.allowed:
            raise RateLimitedError(
                "rate limit exceeded",
                retry_after=result.retry_after or float(window_seconds),
            )
