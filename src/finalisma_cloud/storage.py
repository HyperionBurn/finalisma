"""Finalisma cloud spine — storage interface and SQLite-WAL backend.

This is the seam that makes the storage engine swappable. Business logic
(server handlers, quota enforcement, migration orchestration) imports ONLY the
interface — never ``sqlite3`` directly. A future ``PostgresBackend`` implements
the same ``StorageBackend`` ABC and passes the conformance suite unchanged.

Tenancy is STRUCTURAL: every method that reads or writes tenant-owned data
takes ``tenant_id`` as a required positional parameter (no default), and every
query is scoped by ``WHERE tenant_id = ?``. A caller that omits the tenant
raises ``TypeError``; a wrong tenant is rejected by ``TenantContext`` before the
backend is reached.

Authoritative spec: docs/CLOUD_SPINE_DESIGN.md sections 2-3.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class StorageTransaction(ABC):
    """A scoped unit of work. Backed by BEGIN IMMEDIATE (SQLite) or a Postgres
    transaction. Multi-step operations run inside one of these and commit
    atomically."""

    @abstractmethod
    def execute(self, sql: str, params: tuple = ()) -> Any:
        ...

    @abstractmethod
    def commit(self) -> None:
        ...

    @abstractmethod
    def rollback(self) -> None:
        ...

    def executescript(self, sql: str) -> None:
        """Run a multi-statement SQL script in this transaction.

        SQLite's ``executescript`` implicitly commits any open transaction
        first, so this is only used for schema bootstrap DDL (migrations,
        test fixtures) — never for transactional business logic. Defaults to
        unsupported; the SQLite backend implements it.
        """
        raise NotImplementedError("executescript not supported by this backend")


class StorageBackend(ABC):
    """Transport-engine-agnostic persistence interface.

    Every method that reads or writes tenant-owned data takes ``tenant_id`` as
    a REQUIRED parameter (no default). This is structural tenancy."""

    # --- lifecycle ---
    @abstractmethod
    def initialize(self) -> None:
        """Idempotent schema setup. Creates cloud-plane tables only."""

    @abstractmethod
    @contextmanager
    def transaction(self) -> Iterator[StorageTransaction]:
        ...

    # --- tenant bindings ---
    @abstractmethod
    def create_tenant(self, tenant_id: str, name: str, plan_id: str = "free") -> None:
        ...

    @abstractmethod
    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        ...

    # --- rooms ---
    @abstractmethod
    def bind_room(self, tenant_id: str, room_id: str, coordinator_db_path: str) -> None:
        ...

    @abstractmethod
    def list_rooms(self, tenant_id: str) -> list[dict[str, Any]]:
        ...

    # --- events / cursors ---
    @abstractmethod
    def mirror_event(self, tenant_id: str, room_id: str, event_blob: dict) -> None:
        ...

    @abstractmethod
    def poll_events(self, tenant_id: str, room_id: str, after_seq: int, limit: int) -> list[dict]:
        ...

    # --- quotas / counters ---
    @abstractmethod
    def increment_counter(self, tenant_id: str, counter: str, amount: int = 1) -> int:
        ...

    @abstractmethod
    def get_counter(self, tenant_id: str, counter: str) -> int:
        ...

    @abstractmethod
    def increment_room_counter(self, tenant_id: str, room_id: str, counter: str, amount: int = 1) -> int:
        ...

    @abstractmethod
    def get_room_counter(self, tenant_id: str, room_id: str, counter: str) -> int:
        ...

    # --- rate limits ---
    @abstractmethod
    def check_rate_limit(self, tenant_id: str, room_id: str | None, limit_key: str,
                         max_allowed: int, window_seconds: int) -> tuple[bool, int]:
        ...

    # --- outbox ---
    @abstractmethod
    def enqueue_outbox(self, tenant_id: str, envelope_id: str, recipient: str, payload: str) -> str:
        ...

    @abstractmethod
    def claim_due_outbox(self, tenant_id: str, limit: int) -> list[dict]:
        ...

    # --- audit ---
    @abstractmethod
    def append_audit(self, tenant_id: str, action: str, actor: str, object_id: str, payload: str) -> None:
        ...

    @abstractmethod
    def list_audit(self, tenant_id: str, limit: int = 100) -> list[dict]:
        ...

    # --- migrations ---
    @abstractmethod
    def get_schema_version(self) -> int:
        ...

    @abstractmethod
    def apply_migration(self, migration_id: str, up_sql: str) -> None:
        ...


_CLOUD_INIT_SQL = """
CREATE TABLE IF NOT EXISTS cloud_tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    plan_id TEXT NOT NULL DEFAULT 'free',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cloud_tenant_rooms (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    coordinator_db_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, room_id)
);
CREATE TABLE IF NOT EXISTS cloud_counters (
    tenant_id TEXT NOT NULL,
    counter TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, counter)
);
CREATE TABLE IF NOT EXISTS cloud_room_counters (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    counter TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, room_id, counter)
);
CREATE TABLE IF NOT EXISTS cloud_rate_windows (
    tenant_id TEXT NOT NULL,
    room_id TEXT,
    limit_key TEXT NOT NULL,
    window_start REAL NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, room_id, limit_key, window_start)
);
CREATE TABLE IF NOT EXISTS cloud_outbox (
    entry_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    envelope_id TEXT NOT NULL,
    recipient TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued','claimed','delivered','dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cloud_audit (
    audit_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    object_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cloud_event_mirror (
    mirror_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


class SqliteWalBackend(StorageBackend):
    """SQLite-WAL implementation of the StorageBackend interface.

    One writer, many concurrent readers (WAL). Every tenant query is scoped by
    ``WHERE tenant_id = ?``. ``synchronous = FULL`` on the acknowledged-event
    path so a crash cannot lose an acknowledged event.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(Path(db_path).expanduser().resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    @property
    def state_path(self) -> str:
        """Adapter surface so migrations accept a store OR a backend."""
        return self.db_path

    def _connect(self, *, query_only: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        if query_only:
            connection.execute("PRAGMA query_only = ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(_CLOUD_INIT_SQL)
        finally:
            connection.close()

    def close(self) -> None:
        """Checkpoint and return the database to DELETE journal mode.

        Windows holds WAL -shm/-wal handles on connection objects until GC; a
        checkpoint + DELETE-mode switch releases them so a tempdir can be
        removed cleanly. Call before deleting the database file.
        """
        import gc as _gc
        try:
            connection = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA journal_mode = DELETE")
            finally:
                connection.close()
        except sqlite3.Error:
            pass
        _gc.collect()

    @contextmanager
    def transaction(self) -> Iterator[StorageTransaction]:
        with self._transaction() as connection:
            yield _SqliteTransaction(connection)

    def create_tenant(self, tenant_id: str, name: str, plan_id: str = "free") -> None:
        self._ensure_cloud_schema()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO cloud_tenants(tenant_id, name, plan_id, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id) DO NOTHING",
                (tenant_id, name, plan_id, utc_now_iso()),
            )

    def _ensure_cloud_schema(self) -> None:
        """Idempotent base-schema bootstrap for a not-yet-initialized backend.

        ``initialize()`` is normally called by the test seam. A caller that
        reaches the backend directly (e.g. ``create_tenant`` as the first
        operation on a fresh file) must not explode on a missing table.
        """
        try:
            connection = self._connect(query_only=True)
            try:
                row = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_tenants'"
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            row = None
        if row is None:
            self.initialize()

    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        with self._connect(query_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM cloud_tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        return dict(row) if row else None

    def bind_room(self, tenant_id: str, room_id: str, coordinator_db_path: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO cloud_tenant_rooms(tenant_id, room_id, coordinator_db_path, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, room_id) DO NOTHING",
                (tenant_id, room_id, coordinator_db_path, utc_now_iso()),
            )

    def list_rooms(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect(query_only=True) as conn:
            rows = conn.execute(
                "SELECT * FROM cloud_tenant_rooms WHERE tenant_id = ? ORDER BY created_at",
                (tenant_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mirror_event(self, tenant_id: str, room_id: str, event_blob: dict) -> None:
        import json as _json
        with self._transaction() as conn:
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM cloud_event_mirror WHERE tenant_id = ? AND room_id = ?",
                (tenant_id, room_id),
            ).fetchone()["n"]
            conn.execute(
                "INSERT INTO cloud_event_mirror(mirror_id, tenant_id, room_id, seq, event_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_new_id("mirror"), tenant_id, room_id, next_seq,
                 _json.dumps(event_blob, ensure_ascii=False, sort_keys=True), utc_now_iso()),
            )

    def poll_events(self, tenant_id: str, room_id: str, after_seq: int, limit: int) -> list[dict]:
        import json as _json
        with self._connect(query_only=True) as conn:
            rows = conn.execute(
                "SELECT * FROM cloud_event_mirror WHERE tenant_id = ? AND room_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (tenant_id, room_id, after_seq, limit),
            ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            try:
                item["event"] = _json.loads(item.pop("event_json"))
            except (json.JSONDecodeError, KeyError, TypeError):
                item["event"] = {}
            result.append(item)
        return result

    def increment_counter(self, tenant_id: str, counter: str, amount: int = 1) -> int:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO cloud_counters(tenant_id, counter, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, counter) DO UPDATE SET value = value + excluded.value, updated_at = excluded.updated_at",
                (tenant_id, counter, amount, utc_now_iso()),
            )
            row = conn.execute(
                "SELECT value FROM cloud_counters WHERE tenant_id = ? AND counter = ?",
                (tenant_id, counter),
            ).fetchone()
            return int(row["value"])

    def get_counter(self, tenant_id: str, counter: str) -> int:
        with self._connect(query_only=True) as conn:
            row = conn.execute(
                "SELECT value FROM cloud_counters WHERE tenant_id = ? AND counter = ?",
                (tenant_id, counter),
            ).fetchone()
        return int(row["value"]) if row else 0

    def increment_room_counter(self, tenant_id: str, room_id: str, counter: str, amount: int = 1) -> int:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO cloud_room_counters(tenant_id, room_id, counter, value, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, room_id, counter) DO UPDATE SET value = value + excluded.value, updated_at = excluded.updated_at",
                (tenant_id, room_id, counter, amount, utc_now_iso()),
            )
            row = conn.execute(
                "SELECT value FROM cloud_room_counters WHERE tenant_id = ? AND room_id = ? AND counter = ?",
                (tenant_id, room_id, counter),
            ).fetchone()
            return int(row["value"])

    def get_room_counter(self, tenant_id: str, room_id: str, counter: str) -> int:
        with self._connect(query_only=True) as conn:
            row = conn.execute(
                "SELECT value FROM cloud_room_counters WHERE tenant_id = ? AND room_id = ? AND counter = ?",
                (tenant_id, room_id, counter),
            ).fetchone()
        return int(row["value"]) if row else 0

    def check_rate_limit(self, tenant_id: str, room_id: str | None, limit_key: str,
                         max_allowed: int, window_seconds: int) -> tuple[bool, int]:
        import time as _time
        now = _time.time()
        with self._transaction() as conn:
            # Prune stale windows for this key first.
            conn.execute(
                "DELETE FROM cloud_rate_windows WHERE tenant_id = ? AND room_id IS ? AND limit_key = ? AND window_start < ?",
                (tenant_id, room_id, limit_key, now - window_seconds),
            )
            row = conn.execute(
                "SELECT count FROM cloud_rate_windows WHERE tenant_id = ? AND room_id IS ? AND limit_key = ? AND window_start >= ?",
                (tenant_id, room_id, limit_key, now - window_seconds),
            ).fetchone()
            current = int(row["count"]) if row else 0
            if current >= max_allowed:
                return (False, max(0, max_allowed - current))
            if row is None:
                conn.execute(
                    "INSERT INTO cloud_rate_windows(tenant_id, room_id, limit_key, window_start, count) VALUES (?, ?, ?, ?, 1)",
                    (tenant_id, room_id, limit_key, now),
                )
            else:
                conn.execute(
                    "UPDATE cloud_rate_windows SET count = count + 1 WHERE tenant_id = ? AND room_id IS ? AND limit_key = ? AND window_start >= ?",
                    (tenant_id, room_id, limit_key, now - window_seconds),
                )
            return (True, max(0, max_allowed - (current + 1)))

    def enqueue_outbox(self, tenant_id: str, envelope_id: str, recipient: str, payload: str) -> str:
        import time as _time
        entry_id = _new_id("oeb")
        now = _time.time()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO cloud_outbox(entry_id, tenant_id, envelope_id, recipient, payload_json, status, attempts, next_attempt_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'queued', 0, 0, ?, ?)",
                (entry_id, tenant_id, envelope_id, recipient, payload, utc_now_iso(), utc_now_iso()),
            )
        return entry_id

    def claim_due_outbox(self, tenant_id: str, limit: int) -> list[dict]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM cloud_outbox WHERE tenant_id = ? AND status = 'queued' ORDER BY created_at LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
            ids = [r["entry_id"] for r in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE cloud_outbox SET status = 'claimed', updated_at = ? WHERE entry_id IN ({placeholders})",
                    (utc_now_iso(), *ids),
                )
            # Return with the NEW status ('claimed'), not the pre-update snapshot.
            return [dict(r, **{"status": "claimed"}) for r in rows]

    def append_audit(self, tenant_id: str, action: str, actor: str, object_id: str, payload: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO cloud_audit(audit_id, tenant_id, action, actor, object_id, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_new_id("audit"), tenant_id, action, actor, object_id, payload, utc_now_iso()),
            )

    def list_audit(self, tenant_id: str, limit: int = 100) -> list[dict]:
        with self._connect(query_only=True) as conn:
            rows = conn.execute(
                "SELECT * FROM cloud_audit WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_schema_version(self) -> int:
        with self._connect(query_only=True) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM schema_migrations"
            ).fetchone()
        return int(row["n"]) if row else 0

    def apply_migration(self, migration_id: str, up_sql: str) -> None:
        with self._transaction() as conn:
            applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE migration_id = ?", (migration_id,)
            ).fetchone()
            if applied:
                return
            conn.executescript(up_sql)
            conn.execute(
                "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                (migration_id, utc_now_iso()),
            )


class _SqliteTransaction(StorageTransaction):
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    def execute(self, sql: str, params: tuple = ()) -> Any:
        return self._conn.execute(sql, params)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def executescript(self, sql: str) -> None:
        self._conn.executescript(sql)
