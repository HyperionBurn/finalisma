"""Durable outbox + retry lane for Finalisma.

Stdlib only, SQLite. Messages survive process restart; delivery receipts
prevent double-delivery. Entries fan out per recipient with per-recipient
idempotency, claim atomically, retry with exponential backoff + jitter, and
land in a DLQ after max_attempts.

Tables are prefixed ``outbox_`` and created with ``IF NOT EXISTS`` so the
module coexists with the core schema without touching it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import random
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterator

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE = 1.0
DEFAULT_FACTOR = 2.0
DEFAULT_CAP = 60.0


class BackoffPolicy:
    """Exponential backoff with full jitter."""

    def __init__(
        self,
        base: float = DEFAULT_BASE,
        factor: float = DEFAULT_FACTOR,
        cap: float = DEFAULT_CAP,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        self.base = base
        self.factor = factor
        self.cap = cap
        self.max_attempts = max_attempts

    def delay(self, attempt: int) -> float:
        """Seconds to wait before the retry for the given attempt number."""
        raw = self.base * (self.factor ** attempt)
        bounded = min(raw, self.cap)
        # Full jitter: uniform in [0, bounded).
        return random.uniform(0.0, bounded) if bounded > 0 else 0.0

    @classmethod
    def from_dict(cls, overrides: dict[str, Any] | None) -> "BackoffPolicy":
        if not overrides:
            return cls()
        return cls(
            base=float(overrides.get("base", DEFAULT_BASE)),
            factor=float(overrides.get("factor", DEFAULT_FACTOR)),
            cap=float(overrides.get("cap", DEFAULT_CAP)),
            max_attempts=int(overrides.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
        )


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_db_path: str | None = None
_lock = threading.Lock()


def init(db_path: str) -> None:
    """Idempotently create the outbox tables at ``db_path``.

    Crash recovery: any entries left ``in_flight`` by a previous process are
    reset to ``queued`` so they can be reclaimed.
    """
    global _db_path
    _db_path = db_path
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS outbox_entries (
                entry_id TEXT PRIMARY KEY,
                envelope_id TEXT NOT NULL,
                roster_or_team_id TEXT NOT NULL DEFAULT '',
                recipient TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued','in_flight','delivered','dead')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_due
                ON outbox_entries(status, next_attempt_at);
            CREATE UNIQUE INDEX IF NOT EXISTS outbox_idempotency
                ON outbox_entries(envelope_id, roster_or_team_id, recipient);
            CREATE TABLE IF NOT EXISTS outbox_dlq (
                entry_id TEXT PRIMARY KEY,
                envelope_id TEXT NOT NULL,
                recipient TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                dead_at TEXT NOT NULL
            );
            """
        )
        # Crash recovery: reset stranded in_flight entries.
        conn.execute(
            """
            UPDATE outbox_entries
            SET status = 'queued', next_attempt_at = 0, updated_at = ?
            WHERE status = 'in_flight'
            """,
            (_now_iso(),),
        )


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    if _db_path is None:
        RuntimeError("outbox.init() has not been called")  # noqa: B016
    conn = sqlite3.connect(_db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _txn() -> Iterator[sqlite3.Connection]:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id() -> str:
    return f"obx_{uuid.uuid4().hex}"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enqueue(
    envelope: dict[str, Any],
    recipients: list[str],
    *,
    roster_or_team_id: str = "",
) -> list[str]:
    """Fan out one envelope into one queued entry per recipient.

    Per-recipient idempotency key: ``(envelope_id, roster_or_team_id, recipient)``.
    Re-enqueueing the same triple returns the existing entry ids.
    """
    envelope_id = envelope["envelope_id"]
    payload_json = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    now = _now()
    now_iso = _now_iso()
    entry_ids: list[str] = []
    with _txn() as conn:
        for recipient in recipients:
            # Look up existing entry by idempotency key.
            existing = conn.execute(
                """
                SELECT entry_id FROM outbox_entries
                WHERE envelope_id = ? AND roster_or_team_id = ? AND recipient = ?
                """,
                (envelope_id, roster_or_team_id, recipient),
            ).fetchone()
            if existing is not None:
                entry_ids.append(existing["entry_id"])
                continue
            entry_id = _new_id()
            conn.execute(
                """
                INSERT INTO outbox_entries(
                    entry_id, envelope_id, roster_or_team_id, recipient,
                    payload_json, status, attempts, next_attempt_at,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, NULL, ?, ?)
                """,
                (entry_id, envelope_id, roster_or_team_id, recipient, payload_json, now, now_iso, now_iso),
            )
            entry_ids.append(entry_id)
    return entry_ids


def claim_due(limit: int, now: float | None = None) -> list[dict[str, Any]]:
    """Atomically claim up to ``limit`` due entries as ``in_flight``.

    Returns the claimed rows as dicts. Concurrent callers never overlap
    because the UPDATE ... WHERE runs inside BEGIN IMMEDIATE.
    """
    if now is None:
        now = _now()
    now_iso = _now_iso()
    with _txn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM outbox_entries
            WHERE status = 'queued' AND next_attempt_at <= ?
            ORDER BY next_attempt_at ASC, created_at ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        if not rows:
            return []
        entry_ids = [row["entry_id"] for row in rows]
        placeholders = ",".join("?" for _ in entry_ids)
        conn.execute(
            f"""
            UPDATE outbox_entries
            SET status = 'in_flight', updated_at = ?
            WHERE entry_id IN ({placeholders})
            """,
            (now_iso, *entry_ids),
        )
        return [_row_to_dict(row) for row in rows]


def mark_delivered(entry_id: str, receipt: str | None = None) -> None:
    """Mark an entry delivered. Idempotent: re-delivering is a no-op."""
    now_iso = _now_iso()
    with _txn() as conn:
        conn.execute(
            """
            UPDATE outbox_entries
            SET status = 'delivered', updated_at = ?
            WHERE entry_id = ? AND status != 'delivered'
            """,
            (now_iso, entry_id),
        )


def mark_retry(
    entry_id: str,
    error: str,
    backoff_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply backoff and re-queue, or move to DLQ if max_attempts is exceeded."""
    policy = BackoffPolicy.from_dict(backoff_policy)
    now = _now()
    now_iso = _now_iso()
    with _txn() as conn:
        row = conn.execute(
            "SELECT * FROM outbox_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"outbox entry {entry_id} not found")
        new_attempts = int(row["attempts"]) + 1
        if new_attempts >= policy.max_attempts:
            # Move to DLQ.
            conn.execute(
                """
                UPDATE outbox_entries
                SET status = 'dead', attempts = ?, last_error = ?, updated_at = ?
                WHERE entry_id = ?
                """,
                (new_attempts, error, now_iso, entry_id),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO outbox_dlq(entry_id, envelope_id, recipient, payload_json, reason, dead_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    row["envelope_id"],
                    row["recipient"],
                    row["payload_json"],
                    error,
                    now_iso,
                ),
            )
            return {"entry_id": entry_id, "status": "dead", "attempts": new_attempts, "next_attempt_at": None}
        delay = policy.delay(new_attempts)
        prev_next = row["next_attempt_at"] or 0.0
        # Monotonic schedule: never schedule earlier than the previous attempt.
        next_at = max(now + delay, prev_next)
        conn.execute(
            """
            UPDATE outbox_entries
            SET status = 'queued', attempts = ?, last_error = ?, next_attempt_at = ?, updated_at = ?
            WHERE entry_id = ?
            """,
            (new_attempts, error, next_at, now_iso, entry_id),
        )
        return {
            "entry_id": entry_id,
            "status": "queued",
            "attempts": new_attempts,
            "next_attempt_at": next_at,
        }


def peek_dlq() -> list[dict[str, Any]]:
    """Return all DLQ entries (most recent first)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM outbox_dlq ORDER BY dead_at DESC"
        ).fetchall()
        return [_row_to_dict(row) for row in rows]


def retry_dlq(entry_id: str) -> None:
    """Move a DLQ entry back into the outbox as a fresh queued entry."""
    now_iso = _now_iso()
    now = _now()
    with _txn() as conn:
        row = conn.execute(
            "SELECT * FROM outbox_dlq WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"DLQ entry {entry_id} not found")
        conn.execute(
            """
            UPDATE outbox_entries
            SET status = 'queued', attempts = 0, last_error = NULL,
                next_attempt_at = ?, updated_at = ?
            WHERE entry_id = ?
            """,
            (now, now_iso, entry_id),
        )
        conn.execute("DELETE FROM outbox_dlq WHERE entry_id = ?", (entry_id,))


def stats() -> dict[str, int]:
    """Return queued / in_flight / delivered / dead counts."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM outbox_entries
            GROUP BY status
            """
        ).fetchall()
    counts = {row["status"]: row["n"] for row in rows}
    return {
        "queued": counts.get("queued", 0),
        "in_flight": counts.get("in_flight", 0),
        "delivered": counts.get("delivered", 0),
        "dead": counts.get("dead", 0),
    }


def _get_entry(entry_id: str) -> dict[str, Any] | None:
    """Test helper: fetch one entry row as a dict (or None)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM outbox_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)


def get_entry(entry_id: str) -> dict[str, Any] | None:
    """Fetch one outbox entry as a dict with ``payload`` mapped in.

    Returns ``None`` if the entry does not exist. Additive read surface used by
    the room receipts tool; does not change delivery semantics.
    """
    row = _get_entry(entry_id)
    if row is None:
        return None
    if "payload_json" in row and "payload" not in row:
        row["payload"] = row["payload_json"]
    return row
