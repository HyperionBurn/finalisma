"""Hosted delivery outbox — Stage-2 drain worker for ``cloud_outbox``.

``cloud_outbox`` is the HOSTED delivery outbox: every room send writes one
durable per-recipient entry (via ``enqueue_outbox_in_tx``) and returns a
receipt. This worker is the Stage-2 consumer that was missing — it claims due
entries and delivers each exactly once through a pluggable ``Deliverer``.

Delivery contract (mirrors ``identity/outbox_worker.py`` exactly):

- **At-least-once with idempotency.** A row is claimed with a conditional
  ``UPDATE ... WHERE status IN ('queued', expired-'claimed')`` inside one
  ``BEGIN IMMEDIATE`` transaction, so two concurrent workers (or a restart)
  can never claim the same row; ``delivered`` and ``dead`` are terminal
  states, so a delivered row is never sent twice. A row stuck in ``claimed``
  (worker crashed mid-delivery) is reclaimed after ``lease_seconds`` — that is
  the at-least-once window.
- **Retry with backoff.** A transient failure returns the row to ``queued``
  with ``attempts`` incremented and ``next_attempt_at = now + backoff * attempts``.
  After ``max_attempts`` (or a permanent failure) the row reaches the terminal
  ``dead`` state, so one bad recipient cannot block the queue forever.
- **No secrets in logs.** Only the row id, attempts, and outcome are logged.
  The payload (and anything inside it) is never logged and never put in
  ``last_error`` (only a coarse classification is stored).

How this differs from ``cloud_identity_outbox`` (email): that outbox carries
``to_email``/``subject``/``body`` and its worker sends through a ``Mailer``
(SMTP). This outbox carries ``recipient`` + ``payload_json`` (a room message
envelope) and its worker delivers through a ``Deliverer``. The lifecycle —
lease, claim, retry-with-backoff, terminal ``delivered``/``dead`` — is
identical in shape; only the row shape and the send seam differ.

Runnable as its own process the same way ``identity.outbox_worker`` is.
Without ``WEFT_DELIVERY_SINK`` the worker exits cleanly (code 0) and entries
stay in ``cloud_outbox`` undelivered — the documented behaviour until a sink
is configured, never a crash.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time as _time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Mapping

from weft_cloud.storage import SqliteWalBackend, utc_now_iso

logger = logging.getLogger("weft_cloud.delivery_worker")

STATUS_QUEUED = "queued"
STATUS_CLAIMED = "claimed"
STATUS_DELIVERED = "delivered"
STATUS_DEAD = "dead"

# Permanent vs transient classification. A deliverer raises
# ``PermanentDeliveryError`` for failures that retrying can never fix (bad
# recipient, rejected payload). Anything else is treated as transient.
class PermanentDeliveryError(Exception):
    """Delivery failure that retrying cannot fix."""


class Deliverer(ABC):
    """Pluggable delivery seam — the Stage-2 sender for ``cloud_outbox``."""

    @abstractmethod
    def deliver(self, tenant_id: str, recipient: str, payload_json: str) -> None:
        ...


class LoggingDeliverer(Deliverer):
    """Writes one JSON line per delivered entry to ``sink_path`` (append).

    A real, observable local sink for development and for tests — every
    delivery is recorded on disk, nothing is silently dropped. A remote
    transport implements the same ``Deliverer`` ABC.
    """

    def __init__(self, sink_path: str) -> None:
        self.sink_path = sink_path

    def deliver(self, tenant_id: str, recipient: str, payload_json: str) -> None:
        with open(self.sink_path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps({
                    "tenant_id": tenant_id,
                    "recipient": recipient,
                    "payload": payload_json,
                    "delivered_at": utc_now_iso(),
                }, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )


def _classify_error(exc: BaseException) -> str:
    if isinstance(exc, PermanentDeliveryError):
        return "permanent"
    return "transient"


def _worker_id() -> str:
    return f"wkr_{uuid.uuid4().hex[:12]}"


_DB_PATH_DEFAULT = "./data/weft-cloud.db"


def _resolve_db_path(argv: list[str], environ: Mapping[str, str]) -> str:
    positional = [arg for arg in argv if not arg.startswith("-")]
    if positional:
        return positional[0]
    return (environ.get("WEFT_DB_PATH") or "").strip() or _DB_PATH_DEFAULT


class CloudOutboxDrainer:
    """Claims, delivers, and marks ``cloud_outbox`` rows.

    ``deliverer`` is the Stage-2 sender (a ``Deliverer``); the drainer never
    falls back to enqueuing, so a drain pass can never re-enqueue a row into
    the outbox it is draining.
    """

    def __init__(
        self,
        backend: Any,
        deliverer: Deliverer,
        *,
        batch_size: int = 50,
        max_attempts: int = 5,
        backoff_seconds: float = 60.0,
        lease_seconds: float = 60.0,
        worker_id: str | None = None,
    ) -> None:
        self.backend = backend
        self.deliverer = deliverer
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.lease_seconds = lease_seconds
        self.worker_id = worker_id or _worker_id()

    def claim_due(self, now: float | None = None) -> list[dict]:
        """Atomically claim due undelivered rows for THIS worker.

        The delivery outbox is a SYSTEM lane, not a tenant actor — the drainer
        claims every tenant's due rows in one ``BEGIN IMMEDIATE`` transaction
        (the storage primitive ``claim_due_outbox`` stays tenant-scoped for the
        conformance/tenancy contract; this is the worker's own cross-tenant
        path, mirroring ``identity.outbox_worker.OutboxDrainer.claim_due``).

        Claims queued rows AND rows claimed by a worker whose lease expired.
        A row already claimed by a live worker is not returned here, so it
        cannot be delivered twice.
        """
        now = _time.time() if now is None else now
        with self.backend.transaction() as tx:
            candidates = tx.execute(
                "SELECT entry_id FROM cloud_outbox "
                "WHERE (status = 'queued' OR "
                "       (status = 'claimed' AND claimed_at IS NOT NULL AND claimed_at < ?)) "
                "AND next_attempt_at <= ? ORDER BY created_at LIMIT ?",
                (now - self.lease_seconds, now, self.batch_size),
            ).fetchall()
            ids = [r["entry_id"] for r in candidates]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            tx.execute(
                f"UPDATE cloud_outbox SET status = 'claimed', claimed_at = ?, claimed_by = ?, "
                f"updated_at = ? WHERE entry_id IN ({placeholders}) "
                f"AND (status = 'queued' OR "
                f"     (status = 'claimed' AND claimed_at IS NOT NULL AND claimed_at < ?)) "
                f"AND next_attempt_at <= ?",
                (now, self.worker_id, utc_now_iso(), *ids,
                 now - self.lease_seconds, now),
            )
            owned = tx.execute(
                f"SELECT entry_id, tenant_id, recipient, payload_json, attempts "
                f"FROM cloud_outbox WHERE entry_id IN ({placeholders}) "
                f"AND status = 'claimed' AND claimed_by = ?",
                (*ids, self.worker_id),
            ).fetchall()
        return [dict(r) for r in owned]

    def mark_delivered(self, tenant_id: str, entry_id: str, now: float | None = None) -> None:
        del now  # dispatched_at is written as ISO by the backend
        self.backend.mark_outbox_delivered(tenant_id, entry_id)

    def mark_retry(self, tenant_id: str, entry_id: str, attempts: int,
                   next_attempt_at: float, error: str) -> None:
        self.backend.mark_outbox_retry(tenant_id, entry_id, attempts, next_attempt_at, error)

    def mark_dead(self, tenant_id: str, entry_id: str, attempts: int, error: str) -> None:
        self.backend.mark_outbox_dead(tenant_id, entry_id, attempts, error)

    def drain_once(self, now: float | None = None) -> dict[str, int]:
        """One pass: claim due rows, deliver each exactly once, record outcomes."""
        now = _time.time() if now is None else now
        result = {"claimed": 0, "sent": 0, "retried": 0, "dead": 0}
        for row in self.claim_due(now):
            result["claimed"] += 1
            tenant_id = row["tenant_id"]
            entry_id = row["entry_id"]
            try:
                self.deliverer.deliver(tenant_id, row["recipient"], row["payload_json"])
            except Exception as exc:  # noqa: BLE001 — classified below
                attempts = row["attempts"] + 1
                classification = _classify_error(exc)
                if classification == "permanent" or attempts >= self.max_attempts:
                    self.mark_dead(tenant_id, entry_id, attempts, classification)
                    result["dead"] += 1
                    logger.warning(
                        "outbox delivery terminal entry_id=%s attempts=%d status=%s",
                        entry_id, attempts, classification,
                    )
                else:
                    next_at = now + self.backoff_seconds * attempts
                    self.mark_retry(tenant_id, entry_id, attempts, next_at, classification)
                    result["retried"] += 1
                    logger.info(
                        "outbox delivery retry entry_id=%s attempts=%d next_attempt_at=%s",
                        entry_id, attempts, next_at,
                    )
                continue
            self.mark_delivered(tenant_id, entry_id)
            result["sent"] += 1
            logger.info("outbox delivered entry_id=%s", entry_id)
        return result


def runtime_config(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve runtime settings for the drain worker (argv → env → defaults).

    - ``WEFT_DB_PATH``                    (default ``./data/weft-cloud.db``)
    - ``WEFT_DELIVERY_DRAIN_INTERVAL``    (default ``5``)
    - ``WEFT_DELIVERY_DRAIN_BATCH``       (default ``50``)
    - ``WEFT_DELIVERY_DRAIN_MAX_ATTEMPTS`` (default ``5``)
    - ``WEFT_DELIVERY_DRAIN_BACKOFF``     (default ``60``)
    - ``WEFT_DELIVERY_DRAIN_LEASE``       (default ``60``)
    - ``WEFT_DELIVERY_SINK``              (default ``""`` — disabled)
    """
    argv = list(argv) if argv is not None else []
    environ = os.environ if environ is None else environ

    def _positive(name: str, raw: str | None, default: int | float) -> float:
        if raw is None or raw == "":
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number, got {raw!r}")
        if value <= 0:
            raise ValueError(f"{name} must be positive, got {raw!r}")
        return value

    db_path = _resolve_db_path(argv, environ)
    interval = _positive("WEFT_DELIVERY_DRAIN_INTERVAL",
                         environ.get("WEFT_DELIVERY_DRAIN_INTERVAL"), 5)
    batch_size = int(_positive("WEFT_DELIVERY_DRAIN_BATCH",
                               environ.get("WEFT_DELIVERY_DRAIN_BATCH"), 50))
    max_attempts = int(_positive("WEFT_DELIVERY_DRAIN_MAX_ATTEMPTS",
                                 environ.get("WEFT_DELIVERY_DRAIN_MAX_ATTEMPTS"), 5))
    backoff_seconds = _positive("WEFT_DELIVERY_DRAIN_BACKOFF",
                                environ.get("WEFT_DELIVERY_DRAIN_BACKOFF"), 60)
    lease_seconds = _positive("WEFT_DELIVERY_DRAIN_LEASE",
                              environ.get("WEFT_DELIVERY_DRAIN_LEASE"), 60)

    return {
        "db_path": db_path,
        "interval": interval,
        "batch_size": batch_size,
        "max_attempts": max_attempts,
        "backoff_seconds": backoff_seconds,
        "lease_seconds": lease_seconds,
        "sink_path": (environ.get("WEFT_DELIVERY_SINK") or "").strip(),
    }


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the delivery drain worker. ``--once`` performs a single pass and exits.

    Without ``WEFT_DELIVERY_SINK`` the worker exits cleanly (code 0) and
    entries remain in ``cloud_outbox`` undelivered — the documented behaviour
    until a sink is configured, never a crash.
    """
    argv = list(argv) if argv is not None else []
    environ = os.environ if environ is None else environ
    try:
        cfg = runtime_config(argv, environ)
    except ValueError as exc:
        print(f"finalisma-delivery: {exc}", file=sys.stderr)
        return 2

    if not cfg["sink_path"]:
        print(
            "finalisma-delivery: WEFT_DELIVERY_SINK not set - delivery disabled; "
            "entries stay in cloud_outbox undelivered",
            file=sys.stderr,
        )
        return 0

    backend = SqliteWalBackend(cfg["db_path"])
    backend.initialize()
    try:
        from weft_cloud.identity.schema import ensure_schema
        ensure_schema(backend)
        deliverer = LoggingDeliverer(cfg["sink_path"])
        drainer = CloudOutboxDrainer(
            backend,
            deliverer,
            batch_size=cfg["batch_size"],
            max_attempts=cfg["max_attempts"],
            backoff_seconds=cfg["backoff_seconds"],
            lease_seconds=cfg["lease_seconds"],
        )
        while True:
            result = drainer.drain_once()
            logger.info("delivery pass claimed=%d sent=%d retried=%d dead=%d",
                        result["claimed"], result["sent"],
                        result["retried"], result["dead"])
            if "--once" in argv:
                return 0
            _time.sleep(cfg["interval"])
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
