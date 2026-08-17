"""Small, dependency-free helpers for keeping live outbox leases owned."""

from __future__ import annotations

import threading
from collections.abc import Callable


class LeaseHeartbeat:
    """Renew a database-backed lease while an external side effect is active.

    ``renew`` must return ``True`` while this worker still owns the claim. A
    failed renewal is recorded as lease loss; the caller must then fence its
    completion update and treat the outcome as unknown rather than claiming
    delivery success. Dead processes stop renewing and remain reclaimable after
    the normal lease interval.
    """

    def __init__(self, renew: Callable[[], bool], lease_seconds: float) -> None:
        self._renew = renew
        self._interval = max(0.001, min(float(lease_seconds) / 3.0, 1.0))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.acquired = False
        self.lost = threading.Event()

    def __enter__(self) -> "LeaseHeartbeat":
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
            self._thread.join()
        return False
