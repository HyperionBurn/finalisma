"""Cloud-plane migrations — versioned, forward-only, idempotent.

The hosted service adopts a design-partner's existing schema-v3 coordinator
database in place. The cloud migration is PURELY ADDITIVE: it creates cloud-plane
tables only and never touches coordinator tables (agent_credentials, tasks,
sessions, session_events, room_*, ...). No credentials are fabricated.

Rollback story: down-migrations are NOT provided (data-destructive). The
documented rollback is a pre-upgrade backup of the state file, restored on
failure. The backup IS the rollback.

`apply_migrations` accepts anything with a `.state_path` (a FinalismaStore or a
SqliteWalBackend) and operates on that path directly, so it upgrades a real v3
coordinator database in place.

Authoritative spec: docs/CLOUD_SPINE_DESIGN.md section 4.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Migration:
    migration_id: str
    name: str
    up_sql: str
    # Optional guard: when supplied, ``apply_migrations`` checks it BEFORE
    # running ``up_sql``. If it returns True the migration's effect is already
    # present (e.g. a column added by an earlier code path) and the migration
    # is recorded as applied without re-running its SQL. This keeps
    # ALTER-based migrations forward-only and idempotent, matching the
    # CREATE TABLE IF NOT EXISTS pattern used by the earlier migrations.
    already_applied: Callable[[Any], bool] | None = None


_CLOUD_TABLES_SQL = """
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

_IDENTITY_ACCOUNTS_SQL = """
CREATE TABLE IF NOT EXISTS cloud_identity_accounts (
    account_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    email TEXT NOT NULL,
    salt BLOB NOT NULL,
    password_hash BLOB NOT NULL,
    created_at TEXT NOT NULL,
    email_verified INTEGER NOT NULL DEFAULT 0,
    verification_token_hash TEXT,
    verification_expires_at REAL NOT NULL DEFAULT 0,
    reset_token_hash TEXT,
    reset_expires_at REAL NOT NULL DEFAULT 0,
    FOREIGN KEY(tenant_id) REFERENCES cloud_tenants(tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_identity_accounts_tenant_email
    ON cloud_identity_accounts(tenant_id, email);
CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_accounts_tenant_email_unique
    ON cloud_identity_accounts(tenant_id, email);
"""

_IDENTITY_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS cloud_identity_sessions (
    session_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    role_snapshot TEXT NOT NULL,
    FOREIGN KEY(tenant_id) REFERENCES cloud_tenants(tenant_id),
    FOREIGN KEY(account_id) REFERENCES cloud_identity_accounts(account_id)
);
CREATE INDEX IF NOT EXISTS idx_identity_sessions_account
    ON cloud_identity_sessions(account_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_identity_sessions_token
    ON cloud_identity_sessions(token_hash);
"""

_IDENTITY_MEMBERS_SQL = """
CREATE TABLE IF NOT EXISTS cloud_identity_members (
    tenant_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner','admin','member')),
    joined_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, account_id),
    FOREIGN KEY(tenant_id) REFERENCES cloud_tenants(tenant_id),
    FOREIGN KEY(account_id) REFERENCES cloud_identity_accounts(account_id)
);
CREATE INDEX IF NOT EXISTS idx_identity_members_account
    ON cloud_identity_members(account_id);
"""

_IDENTITY_INVITES_SQL = """
CREATE TABLE IF NOT EXISTS cloud_identity_invites (
    invite_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','member')),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    created_by TEXT NOT NULL,
    FOREIGN KEY(tenant_id) REFERENCES cloud_tenants(tenant_id),
    FOREIGN KEY(created_by) REFERENCES cloud_identity_accounts(account_id)
);
CREATE INDEX IF NOT EXISTS idx_identity_invites_tenant
    ON cloud_identity_invites(tenant_id, email);
"""

_IDENTITY_OUTBOX_SQL = """
CREATE TABLE IF NOT EXISTS cloud_identity_outbox (
    entry_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dispatched_at REAL
);
"""

_ROOM_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS cloud_rooms (
    room_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_agent_id TEXT NOT NULL,
    name TEXT,
    cap INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'forming'
        CHECK(state IN ('forming','active','closed')),
    link_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    cursor_head INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cloud_room_members (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    last_seen REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','stale','left')),
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    actor_token_hash TEXT NOT NULL,
    PRIMARY KEY (tenant_id, room_id, agent_id)
);
CREATE TABLE IF NOT EXISTS cloud_room_links (
    link_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cloud_room_links_tenant ON cloud_room_links(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cloud_room_links_room ON cloud_room_links(room_id);
CREATE TABLE IF NOT EXISTS cloud_room_event_log (
    event_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    origin_agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    message_kind TEXT,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    trace_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(room_id, seq),
    UNIQUE(room_id, origin_agent, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_cloud_room_events_replay ON cloud_room_event_log(room_id, seq);
CREATE INDEX IF NOT EXISTS idx_cloud_room_events_tenant ON cloud_room_event_log(tenant_id);
CREATE TABLE IF NOT EXISTS cloud_room_cursors (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    last_ack_seq INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, room_id, agent_id)
);
CREATE TABLE IF NOT EXISTS cloud_room_groups (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    group_name TEXT NOT NULL,
    PRIMARY KEY (tenant_id, room_id, group_name)
);
CREATE TABLE IF NOT EXISTS cloud_room_group_members (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    group_name TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, room_id, group_name, agent_id)
);
"""


_ROOM_MESSAGE_KIND_SQL = """
-- Forward-only upgrade for databases created before message_kind existed.
-- The cloud_008 migration is guarded by already_applied (column present),
-- so this ALTER only ever runs against a table that actually lacks the column.
ALTER TABLE cloud_room_event_log ADD COLUMN message_kind TEXT;
"""


def _has_room_message_kind(execute: Callable[[str, tuple], Any]) -> bool:
    """True when cloud_room_event_log already has the message_kind column."""
    try:
        row = execute(
            "SELECT 1 FROM pragma_table_info('cloud_room_event_log') WHERE name = 'message_kind'"
        ).fetchone()
        return row is not None
    except Exception:
        return False


MIGRATIONS: list[Migration] = [
    Migration(
        "cloud_001_init",
        "cloud plane bootstrap",
        _CLOUD_TABLES_SQL,
    ),
    Migration(
        "cloud_002_identity_accounts",
        "identity accounts",
        _IDENTITY_ACCOUNTS_SQL,
    ),
    Migration(
        "cloud_003_identity_sessions",
        "identity sessions",
        _IDENTITY_SESSIONS_SQL,
    ),
    Migration(
        "cloud_004_identity_members",
        "identity membership",
        _IDENTITY_MEMBERS_SQL,
    ),
    Migration(
        "cloud_005_identity_invites",
        "identity invites",
        _IDENTITY_INVITES_SQL,
    ),
    Migration(
        "cloud_006_identity_outbox",
        "identity email outbox",
        _IDENTITY_OUTBOX_SQL,
    ),
    Migration(
        "cloud_007_room_tables",
        "cloud room lifecycle + event log + addressing",
        _ROOM_TABLES_SQL,
    ),
    Migration(
        "cloud_008_room_message_kind",
        "cloud room event log message_kind column",
        _ROOM_MESSAGE_KIND_SQL,
        already_applied=_has_room_message_kind,
    ),
]


def apply_migrations(store_or_backend: Any) -> None:
    """Apply forward-only, idempotent cloud migrations to a state database.

    ``store_or_backend`` is anything exposing ``.state_path`` (a
    ``FinalismaStore`` or a ``SqliteWalBackend``). Migrations are tracked in
    ``schema_migrations``; re-running is a no-op. Coordinator tables are never
    altered.

    When passed a ``FinalismaStore``, the migration uses the store's own pooled
    connection lifecycle (via ``_transaction``) so no second raw connection is
    opened against a WAL file — that avoided the Windows file-lock teardown
    flake.
    """
    state_path = Path(store_or_backend.state_path).expanduser().resolve()

    def _run(execute, executescript, commit, rollback) -> None:
        executescript(
            "CREATE TABLE IF NOT EXISTS schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for migration in MIGRATIONS:
            applied = execute(
                "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
                (migration.migration_id,),
            ).fetchone()
            if applied:
                continue
            if migration.already_applied is not None and migration.already_applied(execute):
                # The migration's effect is already present (e.g. a column added
                # by an earlier code path); record it as applied and move on.
                execute(
                    "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                    (migration.migration_id, utc_now_iso()),
                )
                commit()
                continue
            try:
                executescript(migration.up_sql)
                execute(
                    "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                    (migration.migration_id, utc_now_iso()),
                )
                commit()
            except Exception:
                rollback()
                raise

    # Prefer the store's own transaction if it exposes one (FinalismaStore).
    # The store's own close() returns the DB to DELETE journal mode, so no
    # journal-mode handling is done here — a separate connection would race
    # the store's pool and leave a handle open on Windows.
    store_txn = getattr(store_or_backend, "_transaction", None)
    if callable(store_txn):
        with store_txn() as conn:
            _run(conn.execute, conn.executescript, conn.commit, conn.rollback)
        return

    # Fall back to a raw connection (SqliteWalBackend). Use DELETE journal mode
    # for the in-place migration so no WAL -wal/-shm side-files are left behind
    # (they lock the file on Windows teardown). The cloud backend flips to WAL
    # on its own initialize().
    connection = sqlite3.connect(state_path, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        _run(connection.execute, connection.executescript, connection.commit, connection.rollback)
    finally:
        connection.close()
