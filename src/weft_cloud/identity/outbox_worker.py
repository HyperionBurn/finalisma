"""Identity — Stage-2 outbox drain worker (SMTP delivery).

The identity flows (signup verification, password reset, org invite) write a
durable row to ``cloud_identity_outbox`` via ``LocalOutboxMailer`` and return —
that is Stage 1. This worker is Stage 2: it takes undelivered rows and sends
them through the selected ``Mailer`` (``SmtpMailer`` when SMTP is configured),
marking each row's outcome.

Delivery contract:

- **At-least-once with idempotency.** A row is claimed with a conditional
  ``UPDATE ... WHERE status = 'queued'`` inside one ``BEGIN IMMEDIATE``
  transaction, so two concurrent workers (or a restart) can never claim the
  same row; ``sent`` and ``failed`` are terminal states, so a delivered row is
  never sent twice. A row stuck in ``claimed`` (worker crashed mid-send) is
  reclaimed after ``lease_seconds`` — that is the at-least-once window. A live
  worker renews its claim while SMTP is in flight, so a slow but healthy send
  is not reclaimed.
- **Retry with backoff.** A transient failure returns the row to ``queued``
  with ``attempts`` incremented and ``next_attempt_at = now + backoff * attempts``.
  After ``max_attempts`` (or a permanent failure such as a refused recipient) the
  row reaches the terminal ``failed`` state, so one bad address cannot block the
  queue forever.
- **No secrets in logs.** Only the row id, attempts, and outcome are logged.
  The email body and any token inside it are never logged, and are never put in
  ``last_error`` (only a coarse classification is stored).

Runnable as its own process, env-configured the same way
``weft_cloud.service`` and ``weft_cloud.web`` are. The worker reads the SAME
database variable as the service — ``WEFT_DB_PATH`` (default
``./data/weft-cloud.db``) — so it drains the file the service writes to, not an
empty one:

    WEFT_SMTP_HOST=... WEFT_SMTP_PORT=587 \
    WEFT_SMTP_USERNAME=... WEFT_SMTP_PASSWORD=... \
    WEFT_SMTP_FROM=no-reply@example.com \
    WEFT_DB_PATH=./data/weft-cloud.db \
    PYTHONPATH=src python -B -m weft_cloud.identity.outbox_worker

Without SMTP settings the worker exits cleanly and mail stays in the outbox
undelivered — that is the documented behaviour until credentials exist.
"""

from __future__ import annotations

import logging
import os
import smtplib
import sys
import time as _time
import uuid
from typing import Any, Mapping

from weft_cloud.identity.mailer import SmtpMailer, smtp_config_from_env
from weft_cloud.identity.schema import ensure_schema
from weft_cloud.lease import LeaseHeartbeat, call_with_optional_lease
from weft_cloud.storage import SqliteWalBackend, utc_now_iso

logger = logging.getLogger("weft_cloud.identity.outbox_worker")

STATUS_QUEUED = "queued"
STATUS_CLAIMED = "claimed"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

# SMTP failures that may resolve on their own (connection drops, timeouts,
# 4xx responses). A refused recipient or a rejected sender/message is
# permanent — retrying it can only block the queue.
_PERMANENT_FAILURE_TYPES = (
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
)


def _classify_smtp_error(exc: BaseException) -> str:
    if isinstance(exc, _PERMANENT_FAILURE_TYPES):
        return "permanent"
    if isinstance(exc, smtplib.SMTPDataError):
        code = getattr(exc, "smtp_code", None)
        if code is not None and 500 <= int(code) < 600:
            return "permanent"
        return "transient"
    if isinstance(exc, (OSError, ConnectionError, TimeoutError)):
        return "transient"
    if isinstance(exc, smtplib.SMTPException):
        # Includes SMTPServerDisconnected, SMTPConnectError, SMTPHeloError,
        # SMTPAuthenticationError, SMTPResponseException — retryable.
        return "transient"
    return "permanent"


def _worker_id() -> str:
    return f"wkr_{uuid.uuid4().hex[:12]}"


#: The drain worker must read the SAME database the web/service processes write
#: to. ``WEFT_DB_PATH`` is that canonical variable (matching
#: ``weft_cloud.service``), defaulting to ``./data/weft-cloud.db``.
_DB_PATH_DEFAULT = "./data/weft-cloud.db"


def _resolve_db_path(argv: list[str], environ: Mapping[str, str]) -> str:
    """Resolve the database path: argv > WEFT_DB_PATH > default.

    A flag (anything starting with ``-``, e.g. ``--once``) is never treated as a
    path. The variable and default are the same ones ``weft_cloud.service``
    uses, so the worker drains the file the web/service processes write to.
    """
    positional = [arg for arg in argv if not arg.startswith("-")]
    if positional:
        return positional[0]
    return (environ.get("WEFT_DB_PATH") or "").strip() or _DB_PATH_DEFAULT


class OutboxDrainer:
    """Claims, sends, and marks ``cloud_identity_outbox`` rows.

    ``mailer`` is the Stage-2 sender (an ``SmtpMailer``); the drainer never
    falls back to the outbox mailer, so a drain pass can never re-enqueue a row
    into the same outbox it is draining.
    """

    def __init__(
        self,
        backend: Any,
        mailer: Any,
        *,
        batch_size: int = 50,
        max_attempts: int = 5,
        backoff_seconds: float = 60.0,
        lease_seconds: float = 60.0,
    ) -> None:
        self.backend = backend
        self.mailer = mailer
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.lease_seconds = lease_seconds
        self.worker_id = _worker_id()

    def claim_due(self, now: float | None = None) -> list[dict]:
        """Atomically claim due undelivered rows for THIS worker.

        Runs inside one ``BEGIN IMMEDIATE`` transaction: the conditional UPDATE
        with a rowcount guard means a row already claimed by a concurrent worker
        (or on an earlier pass) is not returned here, so it cannot be sent twice.
        """
        now = _time.time() if now is None else now
        with self.backend.transaction() as tx:
            candidates = tx.execute(
                "SELECT entry_id FROM cloud_identity_outbox "
                "WHERE (status = ? OR (status = ? AND claimed_at IS NOT NULL AND claimed_at < ?)) "
                "AND next_attempt_at <= ? ORDER BY created_at LIMIT ?",
                (STATUS_QUEUED, STATUS_CLAIMED, now - self.lease_seconds, now, self.batch_size),
            ).fetchall()
            ids = [r["entry_id"] for r in candidates]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            tx.execute(
                f"UPDATE cloud_identity_outbox SET status = ?, claimed_at = ?, claimed_by = ? "
                f"WHERE entry_id IN ({placeholders}) "
                f"AND (status = ? OR (status = ? AND claimed_at IS NOT NULL AND claimed_at < ?)) "
                f"AND next_attempt_at <= ?",
                (STATUS_CLAIMED, now, self.worker_id, *ids,
                 STATUS_QUEUED, STATUS_CLAIMED, now - self.lease_seconds, now),
            )
            owned = tx.execute(
                f"SELECT entry_id, tenant_id, to_email, subject, body, attempts "
                f"FROM cloud_identity_outbox "
                f"WHERE entry_id IN ({placeholders}) AND status = ? AND claimed_by = ?",
                (*ids, STATUS_CLAIMED, self.worker_id),
            ).fetchall()
        return [dict(r) for r in owned]

    def _mark(self, sql: str, params: tuple,
              lease_seconds: float | None = None,
              now: float | None = None) -> bool:
        if lease_seconds is not None:
            now = _time.time() if now is None else now
            sql += " AND claimed_at IS NOT NULL AND claimed_at >= ?"
            params = (*params, now - lease_seconds)
        with self.backend.transaction() as tx:
            cursor = tx.execute(sql, params)
            updated = cursor.rowcount
            tx.commit()
        return updated == 1

    def mark_sent(self, entry_id: str, now: float | None = None) -> bool:
        now = _time.time() if now is None else now
        return self._mark(
            "UPDATE cloud_identity_outbox SET status = ?, dispatched_at = ?, last_error = NULL "
            "WHERE entry_id = ? AND status = ? AND claimed_by = ?",
            (STATUS_SENT, now, entry_id, STATUS_CLAIMED, self.worker_id),
            lease_seconds=self.lease_seconds,
            now=now,
        )

    def renew_lease(self, entry_id: str) -> bool:
        renew = getattr(self.backend, "renew_identity_outbox_lease", None)
        if not callable(renew):
            return True
        return call_with_optional_lease(
            renew,
            entry_id,
            self.worker_id,
            lease_seconds=self.lease_seconds,
        )

    def _lease_callback(self, entry_id: str):
        if not callable(getattr(self.backend, "renew_identity_outbox_lease", None)):
            logger.warning(
                "identity outbox backend cannot renew leases; refusing send entry_id=%s",
                entry_id,
            )
            return None
        return lambda: self.renew_lease(entry_id)

    def mark_retry(self, entry_id: str, attempts: int, next_attempt_at: float,
                   error: str, now: float | None = None) -> bool:
        return self._mark(
            "UPDATE cloud_identity_outbox SET status = ?, attempts = ?, next_attempt_at = ?, "
            "claimed_at = NULL, claimed_by = NULL, last_error = ? "
            "WHERE entry_id = ? AND status = ? AND claimed_by = ?",
            (STATUS_QUEUED, attempts, next_attempt_at, error, entry_id, STATUS_CLAIMED,
             self.worker_id),
            lease_seconds=self.lease_seconds,
            now=now,
        )

    def mark_failed(self, entry_id: str, attempts: int, error: str,
                    now: float | None = None) -> bool:
        return self._mark(
            "UPDATE cloud_identity_outbox SET status = ?, attempts = ?, "
            "claimed_at = NULL, claimed_by = NULL, last_error = ? "
            "WHERE entry_id = ? AND status = ? AND claimed_by = ?",
            (STATUS_FAILED, attempts, error, entry_id, STATUS_CLAIMED, self.worker_id),
            lease_seconds=self.lease_seconds,
            now=now,
        )

    def drain_once(self, now: float | None = None) -> dict[str, int]:
        """One pass: claim due rows, send each exactly once, record outcomes."""
        now = _time.time() if now is None else now
        result = {"claimed": 0, "sent": 0, "retried": 0, "failed": 0, "lost": 0}
        for row in self.claim_due(now):
            result["claimed"] += 1
            entry_id = row["entry_id"]
            delivery_error: Exception | None = None
            with LeaseHeartbeat(
                self._lease_callback(entry_id), self.lease_seconds
            ) as lease:
                if lease.acquired:
                    try:
                        self.mailer.send(
                            row["tenant_id"], row["to_email"], row["subject"], row["body"]
                        )
                    except Exception as exc:  # noqa: BLE001 — classified below
                        delivery_error = exc
            if not lease.acquired or lease.lost.is_set():
                result["lost"] += 1
                logger.warning("outbox lease lost during send entry_id=%s", entry_id)
                continue
            if delivery_error is not None:
                exc = delivery_error
                attempts = row["attempts"] + 1
                classification = _classify_smtp_error(exc)
                if classification == "permanent" or attempts >= self.max_attempts:
                    if self.mark_failed(
                        entry_id, attempts, classification, now=_time.time()
                    ):
                        result["failed"] += 1
                        logger.warning(
                            "outbox send terminal entry_id=%s attempts=%d status=%s",
                            entry_id, attempts, classification,
                        )
                    else:
                        result["lost"] += 1
                        logger.warning("outbox lease lost entry_id=%s outcome=failed", entry_id)
                else:
                    next_at = now + self.backoff_seconds * attempts
                    if self.mark_retry(
                        entry_id, attempts, next_at, classification, now=_time.time()
                    ):
                        result["retried"] += 1
                        logger.info(
                            "outbox send retry entry_id=%s attempts=%d next_attempt_at=%s",
                            entry_id, attempts, next_at,
                        )
                    else:
                        result["lost"] += 1
                        logger.warning("outbox lease lost entry_id=%s outcome=retry", entry_id)
                continue
            if self.mark_sent(entry_id, now=_time.time()):
                result["sent"] += 1
                logger.info("outbox delivered entry_id=%s", entry_id)
            else:
                result["lost"] += 1
                logger.warning("outbox lease lost entry_id=%s outcome=sent", entry_id)
        return result


def runtime_config(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve runtime settings for the drain worker.

    Same discipline as ``weft_cloud.service.runtime_config`` (argv →
    environment → defaults):

    - ``WEFT_DB_PATH``              (default ``./data/weft-cloud.db``) — the
      SAME variable and default the service uses, so the worker drains the file
      the web/service processes write to.
    - ``WEFT_DRAIN_INTERVAL``        (default ``5``)
    - ``WEFT_DRAIN_BATCH``           (default ``50``)
    - ``WEFT_DRAIN_MAX_ATTEMPTS``    (default ``5``)
    - ``WEFT_DRAIN_BACKOFF``         (default ``60``)

    A malformed interval/batch/attempts raises ``ValueError`` naming the
    variable, mirroring the port validation in the other launchers.
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
    interval = _positive("WEFT_DRAIN_INTERVAL", environ.get("WEFT_DRAIN_INTERVAL"), 5)
    batch_size = int(_positive("WEFT_DRAIN_BATCH", environ.get("WEFT_DRAIN_BATCH"), 50))
    max_attempts = int(
        _positive("WEFT_DRAIN_MAX_ATTEMPTS", environ.get("WEFT_DRAIN_MAX_ATTEMPTS"), 5)
    )
    backoff_seconds = _positive("WEFT_DRAIN_BACKOFF", environ.get("WEFT_DRAIN_BACKOFF"), 60)

    return {
        "db_path": db_path,
        "interval": interval,
        "batch_size": batch_size,
        "max_attempts": max_attempts,
        "backoff_seconds": backoff_seconds,
    }


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the drain worker. ``--once`` performs a single pass and exits.

    Without SMTP settings the worker exits cleanly (code 0) and mail remains in
    ``cloud_identity_outbox`` undelivered — this is the documented behaviour
    until real credentials exist, never a crash.
    """
    argv = list(argv) if argv is not None else []
    environ = os.environ if environ is None else environ
    try:
        cfg = runtime_config(argv, environ)
    except ValueError as exc:
        print(f"finalisma-drain: {exc}", file=sys.stderr)
        return 2

    smtp_config = smtp_config_from_env(environ)
    if smtp_config is None:
        print(
            "finalisma-drain: WEFT_SMTP_HOST not set - SMTP disabled; "
            "mail stays in cloud_identity_outbox undelivered",
            file=sys.stderr,
        )
        return 0

    backend = SqliteWalBackend(cfg["db_path"])
    backend.initialize()
    try:
        ensure_schema(backend)
        mailer = SmtpMailer(**smtp_config)
        drainer = OutboxDrainer(
            backend,
            mailer,
            batch_size=cfg["batch_size"],
            max_attempts=cfg["max_attempts"],
            backoff_seconds=cfg["backoff_seconds"],
        )
        while True:
            result = drainer.drain_once()
            logger.info("drain pass claimed=%d sent=%d retried=%d failed=%d",
                        result["claimed"], result["sent"], result["retried"], result["failed"])
            if "--once" in argv:
                return 0
            _time.sleep(cfg["interval"])
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
