"""Cloud-plane migrations — versioned, forward-only, idempotent.

The hosted service adopts a design-partner's existing schema-v3 coordinator
database in place. The cloud migration is PURELY ADDITIVE: it creates cloud-plane
tables only and never touches coordinator tables (agent_credentials, tasks,
sessions, session_events, room_*, ...). No credentials are fabricated.

Rollback story: down-migrations are NOT provided (data-destructive). The
documented rollback is a pre-upgrade backup of the state file, restored on
failure. The backup IS the rollback.

`apply_migrations` accepts anything with a `.state_path` (a WeftStore or a
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
class GuardedStatement:
    """One SQL statement that is safe to skip when its effect already exists.

    Column-add migrations are split into one ``GuardedStatement`` per ALTER so
    a database where some columns already exist (added out-of-band) is upgraded
    statement-by-statement instead of wedging on ``duplicate column name``.
    Each statement's guard checks ``pragma_table_info`` for its own column.
    """

    sql: str
    already_applied: Callable[[Any], bool]


@dataclass
class Migration:
    migration_id: str
    name: str
    up_sql: str = ""
    # Optional guard: when supplied, ``apply_migrations`` checks it BEFORE
    # running ``up_sql``. If it returns True the migration's effect is already
    # present (e.g. a column added by an earlier code path) and the migration
    # is recorded as applied without re-running its SQL. This keeps
    # ALTER-based migrations forward-only and idempotent, matching the
    # CREATE TABLE IF NOT EXISTS pattern used by the earlier migrations.
    # Only correct for SINGLE-statement migrations — a multi-statement ALTER
    # must use ``statements`` instead, so each ALTER is guarded independently.
    already_applied: Callable[[Any], bool] | None = None
    # Optional per-statement list for multi-statement ALTER migrations. When
    # supplied it takes precedence over ``up_sql``/``already_applied``: every
    # statement is run with its own guard, so a partially-applied database
    # completes the missing columns instead of failing on an existing one.
    statements: list[GuardedStatement] | None = None
    # Optional Python migration body. When supplied it takes precedence over
    # everything else: the callable runs inside the migration's transaction
    # with an ``execute`` function and performs the forward-only, idempotent
    # data rewrite itself. Used by the room-membership recovery, whose per-row
    # collision handling cannot be expressed safely as a single SQL statement.
    up_fn: Callable[[Callable[[str, tuple], Any]], None] | None = None


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
    claimed_at REAL,
    claimed_by TEXT,
    last_error TEXT,
    dispatched_at REAL,
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

_IDENTITY_AGENT_KEYS_SQL = """
CREATE TABLE IF NOT EXISTS cloud_identity_agent_keys (
    key_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    label TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at REAL,
    last_used_at REAL,
    FOREIGN KEY(tenant_id) REFERENCES cloud_tenants(tenant_id),
    FOREIGN KEY(account_id) REFERENCES cloud_identity_accounts(account_id)
);
CREATE INDEX IF NOT EXISTS idx_identity_agent_keys_account
    ON cloud_identity_agent_keys(account_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_identity_agent_keys_token
    ON cloud_identity_agent_keys(token_hash);
"""

_ROOM_RECEIPTS_SQL = """
-- Durable per-recipient consumption state. ``cloud_outbox.status`` remains
-- delivery state (queued/claimed/delivered/dead); ``read_status`` records the
-- recipient's acknowledgement independently so the two lifecycle dimensions
-- cannot be conflated.
--
-- NOTE (2026-08-14): this body is the DEPLOYED canonical shape (f57a792 on
-- production uses ``read_status``). An interim branch version of cloud_013
-- briefly used a ``status`` column under the same migration id; migration ids
-- are the ledger's primary key, so cloud_013 is NEVER mutated again. The
-- cloud_014 migration repairs databases created from that interim shape.
CREATE TABLE IF NOT EXISTS cloud_room_receipts (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    recipient_agent_id TEXT NOT NULL,
    sender_agent_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    read_status TEXT NOT NULL DEFAULT 'queued'
        CHECK(read_status IN ('queued','read')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, room_id, seq, recipient_agent_id)
);
CREATE INDEX IF NOT EXISTS idx_cloud_room_receipts_recipient
    ON cloud_room_receipts(tenant_id, room_id, recipient_agent_id, read_status, seq);
CREATE INDEX IF NOT EXISTS idx_cloud_room_receipts_sender
    ON cloud_room_receipts(tenant_id, room_id, sender_agent_id, seq);
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

def _has_identity_outbox_column(column: str) -> Callable[[Any], bool]:
    """True when ``cloud_identity_outbox`` already has ``column``."""

    def _guard(execute: Callable[[str, tuple], Any]) -> bool:
        try:
            row = execute(
                "SELECT 1 FROM pragma_table_info('cloud_identity_outbox') WHERE name = ?",
                (column,),
            ).fetchone()
            return row is not None
        except Exception:
            return False

    return _guard


# cloud_008 is a multi-statement ALTER. Each ALTER is a separate
# ``GuardedStatement`` so that a database where one of these columns already
# exists (added out-of-band, or left behind by an interrupted run) is upgraded
# for the rest instead of wedging on ``duplicate column name`` forever.
_IDENTITY_OUTBOX_DELIVERY_STATEMENTS = [
    GuardedStatement(
        "ALTER TABLE cloud_identity_outbox ADD COLUMN status TEXT NOT NULL DEFAULT 'queued'",
        _has_identity_outbox_column("status"),
    ),
    GuardedStatement(
        "ALTER TABLE cloud_identity_outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        _has_identity_outbox_column("attempts"),
    ),
    GuardedStatement(
        "ALTER TABLE cloud_identity_outbox ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0",
        _has_identity_outbox_column("next_attempt_at"),
    ),
    GuardedStatement(
        "ALTER TABLE cloud_identity_outbox ADD COLUMN claimed_at REAL",
        _has_identity_outbox_column("claimed_at"),
    ),
    GuardedStatement(
        "ALTER TABLE cloud_identity_outbox ADD COLUMN claimed_by TEXT",
        _has_identity_outbox_column("claimed_by"),
    ),
    GuardedStatement(
        "ALTER TABLE cloud_identity_outbox ADD COLUMN last_error TEXT",
        _has_identity_outbox_column("last_error"),
    ),
]

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
-- The cloud_009 migration is guarded by already_applied (column present),
-- so this ALTER only ever runs against a table that actually lacks the column.
ALTER TABLE cloud_room_event_log ADD COLUMN message_kind TEXT;
"""


_ROOM_CURSOR_RESUME_MARKER_SQL = """
-- Persist the next_seq paging marker per member so a later poll can use it
-- inclusively without confusing it with a normal current-head cursor.
ALTER TABLE cloud_room_cursors ADD COLUMN resume_marker_seq INTEGER;
"""


_ROOM_RECEIPTS_SQL = """
-- Durable per-recipient consumption state. ``cloud_outbox.status`` remains
-- delivery state (queued/claimed/delivered/dead); ``read_status`` records the
-- recipient's acknowledgement independently so the two lifecycle dimensions
-- cannot be conflated.
CREATE TABLE IF NOT EXISTS cloud_room_receipts (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    recipient_agent_id TEXT NOT NULL,
    sender_agent_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    read_status TEXT NOT NULL DEFAULT 'queued'
        CHECK(read_status IN ('queued','read')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, room_id, seq, recipient_agent_id)
);
CREATE INDEX IF NOT EXISTS idx_cloud_room_receipts_recipient
    ON cloud_room_receipts(tenant_id, room_id, recipient_agent_id, read_status, seq);
CREATE INDEX IF NOT EXISTS idx_cloud_room_receipts_sender
    ON cloud_room_receipts(tenant_id, room_id, sender_agent_id, seq);
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


def _has_room_cursor_resume_marker(execute: Callable[[str, tuple], Any]) -> bool:
    """True when cloud_room_cursors has the durable resume-marker column."""
    try:
        row = execute(
            "SELECT 1 FROM pragma_table_info('cloud_room_cursors') WHERE name = 'resume_marker_seq'"
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _rename_receipt_status_column(execute: Callable[[str, tuple], Any]) -> None:
    """cloud_014: converge interim-shape receipt tables onto the deployed shape.

    The deployed canonical column is ``read_status`` (f57a792 on production).
    A branch-local interim body of cloud_013 briefly created ``status`` under
    the SAME migration id; ids are the ledger's primary key, so cloud_013 is
    never mutated — this migration repairs databases that already applied the
    interim shape. Guarded both ways: with ``read_status`` present it is a
    no-op; with only ``status`` present the column is renamed in place
    (SQLite RENAME COLUMN carries data, CHECK constraints, and index
    references with it).
    """
    rows = execute(
        "SELECT name FROM pragma_table_info('cloud_room_receipts')"
    ).fetchall()
    cols = {row["name"] for row in rows}
    if "read_status" in cols or "status" not in cols:
        return
    execute("ALTER TABLE cloud_room_receipts RENAME COLUMN status TO read_status")


def _recover_room_membership_identity(execute: Callable[[str, tuple], Any]) -> None:
    """Rewrite room-membership identity to the authenticated ACCOUNT.

    The regression being repaired: room membership was bound to the SHA-256 of
    the caller's SESSION token (``actor_token_hash``), which rotates on every
    login — so a re-login silently revoked every room the user had joined.
    The fix authorizes by the caller's account. That is only useful to
    existing members if their membership rows can be re-keyed to an account.

    Recovery: the membership row stores ``actor_token_hash``, the same SHA-256
    the identity plane keeps on ``cloud_identity_sessions`` (``token_hash`` —
    sessions are never deleted, only revoked, so expired sessions still
    resolve). Where the join succeeds we recover the TRUE account that joined
    and rewrite ``agent_id`` to it. Rows whose hash matches no session are
    UNRECOVERABLE and are left untouched — they are inert under account-based
    authorization because no account's id equals their ``agent_id``.

    Collision handling: two rows in one room may resolve to the same account
    (a forged join plus the real join). The earliest row wins; later rows that
    duplicate an already-claimed account were never legitimate identities and
    are deleted, along with their cursor row. A recovered member's cursor is
    re-keyed to the account so its durable position survives.

    The room's OWNER is recovered from the owner's own membership row (the row
    whose ``agent_id`` equals ``cloud_rooms.owner_agent_id`` and whose session
    hash resolves), so the owner keeps close/revoke powers under the new model.

    Forward-only and idempotent: rows already keyed on the true account are
    skipped, and re-running the migration is a no-op.
    """
    # 0. Recover the room OWNER FIRST, while the membership row still carries
    # the (possibly client-supplied) owner_agent_id: the owner is the account
    # whose session minted the room, found via the owner's own membership row.
    owner_rows = execute(
        "SELECT r.room_id, r.tenant_id, r.owner_agent_id "
        "FROM cloud_rooms r "
        "JOIN cloud_room_members m "
        "  ON m.room_id = r.room_id AND m.tenant_id = r.tenant_id "
        "  AND m.agent_id = r.owner_agent_id AND m.status = 'active'"
    ).fetchall()
    for row in owner_rows:
        account = execute(
            "SELECT s.account_id FROM cloud_identity_sessions s "
            "WHERE s.token_hash = ("
            "  SELECT actor_token_hash FROM cloud_room_members m "
            "  WHERE m.tenant_id = ? AND m.room_id = ? AND m.agent_id = ? "
            "  AND m.status = 'active' LIMIT 1)",
            (row["tenant_id"], row["room_id"], row["owner_agent_id"]),
        ).fetchone()
        if account is None or account["account_id"] == row["owner_agent_id"]:
            continue
        execute(
            "UPDATE cloud_rooms SET owner_agent_id = ? "
            "WHERE tenant_id = ? AND room_id = ?",
            (account["account_id"], row["tenant_id"], row["room_id"]),
        )

    # 1. Rewrite each member's identity to the true account.
    rows = execute(
        "SELECT tenant_id, room_id, agent_id, actor_token_hash, joined_at "
        "FROM cloud_room_members "
        "WHERE actor_token_hash IN (SELECT token_hash FROM cloud_identity_sessions) "
        "ORDER BY joined_at, rowid"
    ).fetchall()
    claimed: set[tuple[str, str, str]] = set()
    for row in rows:
        account = execute(
            "SELECT account_id FROM cloud_identity_sessions WHERE token_hash = ?",
            (row["actor_token_hash"],),
        ).fetchone()
        if account is None:
            continue
        account_id = account["account_id"]
        tenant_id, room_id, agent_id = row["tenant_id"], row["room_id"], row["agent_id"]
        key = (tenant_id, room_id, account_id)
        if agent_id == account_id:
            # Already the true identity — nothing to do.
            claimed.add(key)
            continue
        if key in claimed:
            # A later row resolving to an already-claimed account is a forged
            # duplicate; drop it (and its ghost cursor) so it cannot hold a seat.
            execute(
                "DELETE FROM cloud_room_members "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, agent_id),
            )
            execute(
                "DELETE FROM cloud_room_cursors "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, agent_id),
            )
            continue
        execute(
            "UPDATE cloud_room_members SET agent_id = ? "
            "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
            (account_id, tenant_id, room_id, agent_id),
        )
        if execute(
            "SELECT 1 FROM cloud_room_cursors "
            "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
            (tenant_id, room_id, account_id),
        ).fetchone() is None:
            execute(
                "UPDATE cloud_room_cursors SET agent_id = ? "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (account_id, tenant_id, room_id, agent_id),
            )
        else:
            execute(
                "DELETE FROM cloud_room_cursors "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, agent_id),
            )
        claimed.add(key)
def _has_cloud_outbox_column(column: str) -> Callable[[Callable[[str, tuple], Any]], bool]:
    """True when cloud_outbox already has ``column``."""

    def _guard(execute: Callable[[str, tuple], Any]) -> bool:
        try:
            row = execute(
                "SELECT 1 FROM pragma_table_info('cloud_outbox') WHERE name = ?",
                (column,),
            ).fetchone()
            return row is not None
        except Exception:
            return False

    return _guard


_CLOUD_OUTBOX_LIFECYCLE_STATEMENTS = [
    GuardedStatement(
        "ALTER TABLE cloud_outbox ADD COLUMN claimed_at REAL",
        _has_cloud_outbox_column("claimed_at"),
    ),
    GuardedStatement(
        "ALTER TABLE cloud_outbox ADD COLUMN claimed_by TEXT",
        _has_cloud_outbox_column("claimed_by"),
    ),
    GuardedStatement(
        "ALTER TABLE cloud_outbox ADD COLUMN last_error TEXT",
        _has_cloud_outbox_column("last_error"),
    ),
    GuardedStatement(
        "ALTER TABLE cloud_outbox ADD COLUMN dispatched_at REAL",
        _has_cloud_outbox_column("dispatched_at"),
    ),
]


_CLOUD_OUTBOX_CLAIM_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_cloud_outbox_claim
    ON cloud_outbox(status, next_attempt_at, claimed_at, created_at);
CREATE INDEX IF NOT EXISTS idx_cloud_identity_outbox_claim
    ON cloud_identity_outbox(status, next_attempt_at, claimed_at, created_at);
"""


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
        "cloud_008_identity_outbox_delivery",
        "identity email outbox delivery state (status/attempts/backoff)",
        statements=_IDENTITY_OUTBOX_DELIVERY_STATEMENTS,
    ),
    Migration(
        "cloud_009_room_message_kind",
        "cloud room event log message_kind column",
        _ROOM_MESSAGE_KIND_SQL,
        already_applied=_has_room_message_kind,
    ),
    # Two parallel branches each authored a migration numbered cloud_010: the
    # membership-identity recovery and the hosted-outbox lifecycle. They are
    # separate migrations and both are required, so the outbox one is renumbered
    # to cloud_011. Migration ids are the ledger's primary key — two rows sharing
    # an id would make "has this run?" unanswerable.
    Migration(
        "cloud_010_room_membership_account",
        "bind room membership to the authenticated account (recoverable from session table)",
        up_fn=_recover_room_membership_identity,
    ),
    Migration(
        "cloud_011_cloud_outbox_lifecycle",
        "hosted delivery outbox completion lifecycle (lease/retry/delivered/dead)",
        statements=_CLOUD_OUTBOX_LIFECYCLE_STATEMENTS,
    ),
    Migration(
        "cloud_012_identity_agent_keys",
        "agent API keys — long-lived, revocable config-file credentials (SHA-256 at rest)",
        _IDENTITY_AGENT_KEYS_SQL,
    ),
    Migration(
        "cloud_013_room_receipts",
        "per-recipient room message receipts with queued->read lifecycle "
        "(the send response's receipt status is no longer a hardcoded literal; "
        "a recipient's ack past an event's seq marks its receipt read)",
        _ROOM_RECEIPTS_SQL,
    ),
    Migration(
        "cloud_014_room_receipts_status_rename",
        "repair databases created from the interim cloud_013 body that named "
        "the consumption column 'status'; the deployed canonical column is "
        "'read_status'. Guarded both ways: a no-op when read_status exists.",
        up_fn=_rename_receipt_status_column,
    ),
    Migration(
        "cloud_015_outbox_claim_indexes",
        "indexes for queued/retry/lease claim selectors",
        _CLOUD_OUTBOX_CLAIM_INDEXES_SQL,
    ),
    Migration(
        "cloud_016_room_cursor_resume_marker",
        "durable per-member room cursor resume marker",
        _ROOM_CURSOR_RESUME_MARKER_SQL,
        already_applied=_has_room_cursor_resume_marker,
    ),
]


def apply_migrations(store_or_backend: Any) -> None:
    """Apply forward-only, idempotent cloud migrations to a state database.

    ``store_or_backend`` is anything exposing ``.state_path`` (a
    ``WeftStore`` or a ``SqliteWalBackend``). Migrations are tracked in
    ``schema_migrations``; re-running is a no-op. Coordinator tables are never
    altered.

    When passed a ``WeftStore``, the migration uses the store's own pooled
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
                if migration.up_fn is not None:
                    # Python body: runs inside the migration's transaction with
                    # an ``execute`` function (see up_fn docstring).
                    migration.up_fn(execute)
                elif migration.statements is not None:
                    # Multi-statement ALTER: run each statement with its own
                    # guard. A column that already exists is skipped instead of
                    # raising ``duplicate column name``, so a partially-applied
                    # database completes instead of wedging forever.
                    for statement in migration.statements:
                        if statement.already_applied(execute):
                            continue
                        execute(statement.sql)
                else:
                    executescript(migration.up_sql)
                execute(
                    "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                    (migration.migration_id, utc_now_iso()),
                )
                commit()
            except Exception:
                rollback()
                raise

    # Prefer the store's own transaction if it exposes one (WeftStore).
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
