"""Small, dependency-free helpers for keeping live outbox leases owned."""

from __future__ import annotations

import threading
from collections.abc import Callable
from inspect import Parameter, signature


def call_with_optional_lease(
    method: Callable[..., bool],
    *args,
    lease_seconds: float,
    now: float | None = None,
) -> bool:
    """Call modern or legacy lease methods without retrying side effects.

    Older storage implementations do not accept lease metadata. Signature
    inspection keeps those calls compatible without catching a ``TypeError``
    raised from inside a side-effecting method and invoking it twice.
    """
    try:
        parameters = tuple(signature(method).parameters.values())
    except (TypeError, ValueError):
        parameters = ()
    accepts_kwargs = any(p.kind is Parameter.VAR_KEYWORD for p in parameters)
    accepted = {
        p.name for p in parameters
        if p.kind in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY)
    }
    kwargs = {}
    if accepts_kwargs or "lease_seconds" in accepted:
        kwargs["lease_seconds"] = lease_seconds
    if now is not None and (accepts_kwargs or "now" in accepted):
        kwargs["now"] = now
    return bool(method(*args, **kwargs))


class LeaseHeartbeat:
    """Renew a database-backed lease while an external side effect is active.

    ``renew`` must return ``True`` while this worker still owns the claim. A
    failed or unavailable renewal is recorded as lease loss; the caller must
    then fence its completion update and treat the outcome as unknown rather
    than claiming delivery success. Dead processes stop renewing and remain
    reclaimable after the normal lease interval.
    """

    def __init__(self, renew: Callable[[], bool] | None, lease_seconds: float) -> None:
        self._renew = renew
        self._interval = max(0.001, min(float(lease_seconds) / 3.0, 1.0))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.acquired = False
        self.lost = threading.Event()

    def __enter__(self) -> "LeaseHeartbeat":
        if self._renew is None:
            # A worker that cannot prove ownership must never perform an
            # external side effect. Fail closed for legacy/custom backends
            # without a renewal primitive.
            self.acquired = False
            self.lost.set()
            return self
        try:
            self.acquired = bool(self._renew())
        except Exception:  # noqa: BLE001 - a failed renewal is lease loss
            self.acquired = False
        if not self.acquired:
            self.lost.set()
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not self._renew():
                    self.lost.set()
                    return
            except Exception:  # noqa: BLE001 - a failed renewal is lease loss
                self.lost.set()
                return

    def __exit__(self, exc_type, exc_value, traceback) -> bool:  # noqa: ANN001
        self._stop.set()
        if self._thread is not None:
            # A blocked database callback must not hold the delivery worker
            # hostage forever. The thread is daemonized and its next SQL
            # update is owner/status fenced, so a bounded join is safe.
            self._thread.join(timeout=max(1.0, self._interval * 2.0))
        return False
