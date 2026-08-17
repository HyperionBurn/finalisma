"""Cloud-plane rate limiting — a configurable seam.

v1 uses ``cloud_rate_windows`` rows (SQLite) with windowed cleanup. Wave I can
swap in a Redis-backed limiter without touching enforcement code, because the
enforcement calls ``limiter.check()`` / ``limiter.enforce()`` only.

Authoritative spec: docs/CLOUD_SPINE_DESIGN.md section 5.3.

Auth endpoints (signup, signin, reset-request, refresh) are enforced through the SAME
``RateLimiter`` here — one mechanism, reused. ``enforce_auth_rate_limit``
maps each public auth action onto a fixed-window limit keyed on the client IP
and on the normalized email. The email tier is counted REGARDLESS of whether
the account exists, so a throttled unknown address is byte-identical to a
throttled known one and the refusal cannot enumerate accounts.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Mapping

# Synthetic tenant that owns auth rate-limit rows. Auth endpoints run before
# any tenant exists (signup) and must share one namespace, so they never touch
# a real tenant's budget.
AUTH_LIMIT_TENANT = "__auth__"

# Default auth-endpoint limits (fixed window). ``ip`` bounds how much one
# source can do; ``email`` is the per-account tier, counted for known AND
# unknown addresses alike so it cannot be probed as an existence oracle.
# Defaults are deliberately generous for legitimate use (a person mistyping
# and retrying) and are overridable per instance for deployments and tests.
DEFAULT_AUTH_RATE_LIMITS: dict[str, dict[str, Any]] = {
    "signup": {"ip": 20, "email": 5, "window_seconds": 900},
    "signin": {"ip": 40, "email": 20, "window_seconds": 900},
    "reset_request": {"ip": 10, "email": 3, "window_seconds": 900},
    "refresh": {"ip": 60, "email": 60, "window_seconds": 900},
}


def _client_ip(handler: Any) -> str:
    address = getattr(handler, "client_address", None)
    if address:
        return str(address[0])
    return "unknown"


def _email_limit_key(email: str) -> str:
    """SHA-256 of the normalized email so no raw address lands in the rate table.

    The digest is computed BEFORE any existence check so its cost is identical
    for known and unknown addresses — it cannot become a timing signal.
    """
    normalized = email.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def enforce_auth_rate_limit(backend: Any, handler: Any, action: str,
                            email: str | None = None,
                            limits: Mapping[str, Any] | None = None) -> None:
    """Enforce the shared auth limits for one public endpoint.

    ``action`` is one of the DEFAULT_AUTH_RATE_LIMITS keys. ``limits`` (the
    per-instance override, or None) is merged over the defaults so callers can
    tune thresholds without forking the mechanism. Two tiers are checked, in a
    fixed order, for every request:

      - client IP  — ``{action}:ip:{ip}``
      - email      — ``{action}:email:{sha256(email)}``, when ``email`` given,
                     and counted whether or not the account exists.

    Either tier tripping raises ``RateLimitedError``; because both tiers run
    before any existence-dependent branch, the refusal is byte-identical for a
    known and an unknown address. This is the same ``RateLimiter`` the room
    message budget uses — one mechanism, reused.
    """
    config = dict(DEFAULT_AUTH_RATE_LIMITS[action])
    if limits:
        for key, value in limits.get(action, {}).items():
            config[key] = value
    window = int(config["window_seconds"])
    limiter = RateLimiter()
    limiter.enforce(backend, AUTH_LIMIT_TENANT, None,
                    f"{action}:ip:{_client_ip(handler)}",
                    int(config["ip"]), window)
    if email:
        limiter.enforce(backend, AUTH_LIMIT_TENANT, None,
                        f"{action}:email:{_email_limit_key(email)}",
                        int(config["email"]), window)


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
