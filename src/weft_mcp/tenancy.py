"""Tenancy / identity boundary layer for Weft.

This module owns the multi-tenant isolation boundary: orgs, org membership,
per-org actor-key derivation, and resource claims. It manages its own
``tenancy_*`` tables in the same SQLite file as ``core.py`` and never touches
core tables.

Design seam for OAuth/OIDC (see docs/IDENTITY_OIDC.md):
    Functions take ``org_id`` and ``agent_id`` -- never raw tokens. An
    OAuth/OIDC resource server sits *in front* of this module: it validates
    the external token, maps the external ``sub`` to an internal
    ``(org_id, agent_id)`` pair, and then calls into tenancy. No plaintext
    OAuth tokens ever reach this module.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import sqlite3
import threading
import uuid
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScopeError(PermissionError):
    """Raised when an actor fails scope enforcement."""


# ---------------------------------------------------------------------------
# Internal connection helpers (stdlib only, no core.py dependency)
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(
        db_path,
        timeout=15,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextlib.contextmanager
def _transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenancy_orgs (
    org_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenancy_org_members (
    org_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    joined_at TEXT NOT NULL,
    PRIMARY KEY (org_id, agent_id),
    FOREIGN KEY (org_id) REFERENCES tenancy_orgs(org_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tenancy_claims (
    org_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    actor_key TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (org_id, resource_type, resource_id),
    FOREIGN KEY (org_id) REFERENCES tenancy_orgs(org_id) ON DELETE CASCADE
);
"""


# ---------------------------------------------------------------------------
# init -- idempotent
# ---------------------------------------------------------------------------

def init(db_path: str) -> None:
    """Idempotently create tenancy tables.

    Safe to call repeatedly and safe to call against a database that already
    has ``core.py`` tables -- only ``tenancy_*`` tables are touched.
    """
    with _LOCK:
        with _transaction(db_path) as connection:
            connection.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# Org CRUD
# ---------------------------------------------------------------------------

def create_org(db_path: str, name: str) -> str:
    """Create an org and return its ``org_id``."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("org name must be a non-empty string")
    org_id = "org_" + uuid.uuid4().hex
    with _transaction(db_path) as connection:
        connection.execute(
            "INSERT INTO tenancy_orgs(org_id, name, created_at) VALUES (?, ?, ?)",
            (org_id, name.strip(), _utc_now()),
        )
    return org_id


def list_orgs(db_path: str) -> list[dict[str, Any]]:
    """Return all orgs, oldest first."""
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            "SELECT org_id, name, created_at FROM tenancy_orgs ORDER BY created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

def add_member(db_path: str, org_id: str, agent_id: str, role: str = "viewer") -> None:
    """Add (or re-add / update) a member in an org."""
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("agent_id must be a non-empty string")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role must be a non-empty string")
    with _transaction(db_path) as connection:
        _require_org(connection, org_id)
        connection.execute(
            """
            INSERT INTO tenancy_org_members(org_id, agent_id, role, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(org_id, agent_id) DO UPDATE SET role = excluded.role
            """,
            (org_id, agent_id, role.strip(), _utc_now()),
        )


def remove_member(db_path: str, org_id: str, agent_id: str) -> None:
    """Remove a member from an org. No-op if they were not a member."""
    with _transaction(db_path) as connection:
        connection.execute(
            "DELETE FROM tenancy_org_members WHERE org_id = ? AND agent_id = ?",
            (org_id, agent_id),
        )


def is_member(db_path: str, org_id: str, agent_id: str) -> bool:
    """Return True iff ``agent_id`` is a member of ``org_id``."""
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT 1 FROM tenancy_org_members WHERE org_id = ? AND agent_id = ?",
            (org_id, agent_id),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def list_members(db_path: str, org_id: str) -> list[dict[str, Any]]:
    """List members of an org."""
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            "SELECT agent_id, role, joined_at FROM tenancy_org_members WHERE org_id = ? ORDER BY joined_at ASC",
            (org_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Actor key derivation
# ---------------------------------------------------------------------------

def derive_actor_key(org_id: str, agent_id: str) -> str:
    """Derive a per-org, per-agent scoped key.

    The key is ``SHA-256(org_id + agent_id)``. It is deterministic and
    contains no plaintext token material, so it can be stored and compared
    safely (mirrors the ``agent_credentials.token_hash`` approach in
    ``core.py``).
    """
    return hashlib.sha256((org_id + agent_id).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------

def assert_scope(db_path: str, org_id: str, agent_id: str, actor_key_hex: str) -> None:
    """Validate that ``actor_key_hex`` matches the derived key for the
    (org_id, agent_id) pair AND that the agent is a member of the org.

    Raises ``ScopeError`` on any mismatch. This is the per-request gate.
    """
    expected = derive_actor_key(org_id, agent_id)
    if not _secure_compare(expected, actor_key_hex):
        raise ScopeError("actor_key mismatch for org")
    if not is_member(db_path, org_id, agent_id):
        raise ScopeError("agent is not a member of org")


# ---------------------------------------------------------------------------
# Resource claims (per-org scoped ownership records)
# ---------------------------------------------------------------------------

def claim_resource(
    db_path: str,
    org_id: str,
    resource_type: str,
    resource_id: str,
    actor_id: str,
) -> None:
    """Record that ``actor_id`` (already scope-checked by caller) owns a
    resource within ``org_id``. The stored ``actor_key`` is the derived key.
    """
    actor_key = derive_actor_key(org_id, actor_id)
    with _transaction(db_path) as connection:
        _require_org(connection, org_id)
        connection.execute(
            """
            INSERT INTO tenancy_claims(org_id, resource_type, resource_id, actor_key, claimed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(org_id, resource_type, resource_id) DO UPDATE SET
                actor_key = excluded.actor_key,
                claimed_at = excluded.claimed_at
            """,
            (org_id, resource_type, resource_id, actor_key, _utc_now()),
        )


def list_claims(db_path: str, org_id: str) -> list[dict[str, Any]]:
    """List all claims for an org."""
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            "SELECT resource_type, resource_id, actor_key, claimed_at FROM tenancy_claims WHERE org_id = ? ORDER BY claimed_at ASC",
            (org_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_org(connection: sqlite3.Connection, org_id: str) -> None:
    row = connection.execute(
        "SELECT 1 FROM tenancy_orgs WHERE org_id = ?", (org_id,)
    ).fetchone()
    if row is None:
        raise ValueError("org '" + org_id + "' does not exist")


def _secure_compare(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0
