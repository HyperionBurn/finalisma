"""Cloud-plane Room service — the product object behind the hosted SaaS.

This is the MISSING PIECE that turns the coordinator primitives into a product:
a person signs up (identity plane), creates a room, gets ONE link, pastes it
into several agents, and those agents all talk to each other through it.

``CloudRoomService`` composes the Wave F storage backend with the Wave G
identity modules. It owns the room_-prefixed tables in the cloud database and
exposes the room lifecycle + addressing surface that the HTTP service drives.

Tenancy is structural: every call takes ``tenant_id`` and the storage backend
scopes every query by ``WHERE tenant_id = ?``. Roles are enforced through the
``SessionContext`` the caller supplies (layer 1) plus the membership row in
``cloud_identity_members`` (layer 2) — the same defence-in-depth pattern as
``orgs.py``.

Authoritative spec: docs/ROOMS_DESIGN.md (coordinator room model) adapted to
the cloud storage interface.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time as _time
import uuid
from contextlib import nullcontext
from typing import Any, Callable

from weft_cloud.storage import StorageBackend, utc_now_iso
from weft_cloud.origin import DEFAULT_PUBLIC_ORIGIN, configured_origin

from .identity.context import SessionContext, require_db_role, require_db_role_in_tx
from .identity.tokens import AuthError, hash_token
from .quotas import (
    QuotaError,
    bind_room_with_quota_in_tx,
    enforce_events_per_month,
    increment_room_member_counter,
    plan_limits,
    resolve_plan,
    validate_room_cap,
)
from .rate_limit import RateLimiter

def public_origin(origin: str | None = None) -> str:
    """Resolve the configured public origin for shareable-link URLs.

    Explicit argument wins, then the ``WEFT_PUBLIC_ORIGIN`` env var, then
    the local dev default. A trailing slash is stripped so callers can build
    ``{origin}/j/{token}`` directly.
    """
    return configured_origin(origin, default=DEFAULT_PUBLIC_ORIGIN)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _token_hash(token: str) -> str:
    if not isinstance(token, str) or len(token) < 16 or len(token) > 512:
        raise ValueError("token must be a non-empty opaque capability")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_MESSAGE_KIND_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_LINK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ROOM_NAME_MAX_LENGTH = 160

# Presence freshness threshold (seconds). A member whose last_seen is older
# than this is displayed "stale" in room_info. ONE named constant — every
# presence surface MUST agree, so the literal is never duplicated (the status
# surface and target routing silently diverged before the staleloss fix).
# NOTE: this governs DISPLAY and liveness bookkeeping only. Deliverability is
# keyed on membership, never on this window — an idle member still receives
# its mail (see _route_targets; the P0 staleloss fix).
ROOM_STALE_AFTER_SECONDS = 1800.0

# Liveness write throttle (seconds). ANY authenticated room call by a member
# proves they are using the room, so the read paths refresh last_seen too.
# The write is bounded to at most one per window so a busy poll loop does not
# serialize the room behind SQLite's single writer; an active member's age
# stays <= window, a tiny fraction of ROOM_STALE_AFTER_SECONDS.
ROOM_LIVENESS_TOUCH_INTERVAL = 5.0


def _validate_link_id(link_id: Any) -> None:
    """Validate a ``link_id`` is a well-formed control-plane identifier.

    A malformed ``link_id`` is the caller's error (``invalid_argument``) and is
    refused BEFORE any room/link lookup, so it can never be confused with a
    missing link. Only the format is checked here — existence and ownership are
    decided by the caller against ``cloud_room_links``.
    """
    if not isinstance(link_id, str) or not _LINK_ID_RE.match(link_id):
        raise RoomError("invalid_argument", "link_id must be 1-128 safe identifier characters")


def _validate_message_kind(value: Any, field: str = "message_kind") -> str | None:
    """Validate a sender-set ``message_kind`` (optional, lowercase slug).

    ``None`` is allowed (message_kind is optional). Otherwise it must be a
    lowercase ``[a-z0-9_-]`` string of at most 32 characters. The error names
    the argument so callers get a clear message.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not _MESSAGE_KIND_RE.match(value):
        raise RoomError(
            "invalid_argument",
            f"{field} must be an optional string of 1-32 lowercase "
            "characters matching [a-z0-9_-]",
        )
    return value


# Idempotency keys are validated at the request boundary, BEFORE they are used
# as a database key anywhere. Max 256 characters: room for namespaced keys
# (e.g. ``dispatch:<task_id>``) and a full UUID, while bounding the stored
# bytes per row; a non-string (list/dict/int/bool/float) previously crashed
# the SQLite bind (500) or was silently type-confused into an int key.
_IDEMPOTENCY_KEY_MAX_LEN = 256


def _validate_idempotency_key(value: Any) -> str | None:
    """Validate a caller-supplied ``idempotency_key`` (optional, bounded string).

    ``None`` keeps the current "no idempotency" behaviour. Otherwise the key
    must be a non-empty STRING of at most ``_IDEMPOTENCY_KEY_MAX_LEN``
    characters. The error message is static and NEVER echoes the caller's
    value (a 64KB key must not be reflected into the error body or logs).
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _IDEMPOTENCY_KEY_MAX_LEN:
        raise RoomError(
            "invalid_argument",
            f"idempotency_key must be a non-empty string of at most "
            f"{_IDEMPOTENCY_KEY_MAX_LEN} characters",
        )
    return value


def _validate_message_kinds(value: Any) -> list[str] | None:
    """Validate the optional ``message_kinds`` poll filter.

    ``None`` means "no filter" (return everything). Otherwise it must be a
    list of message-kind strings, each validated by ``_validate_message_kind``.
    """
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise RoomError(
            "invalid_argument",
            "message_kinds must be an optional list of message_kind strings",
        )
    if len(value) > _MESSAGE_KINDS_MAX_ITEMS:
        raise RoomError(
            "invalid_argument",
            f"message_kinds must contain at most {_MESSAGE_KINDS_MAX_ITEMS} entries",
        )
    validated: list[str] = []
    for entry in value:
        v = _validate_message_kind(entry, "message_kinds")
        if v is not None:
            validated.append(v)
    return validated


# Poll filter bound: one IN() placeholder is built per entry (rooms.poll), and
# SQLite rejects more than SQLITE_MAX_VARIABLE_NUMBER (32 766) variables. An
# unbounded list therefore turned a big filter into an OperationalError -> 500.
# 64 is the documented cap, mirroring the entry_ids <= 200 precedent.
_MESSAGE_KINDS_MAX_ITEMS = 64

# Send addressing bounds: one unroutable spec per entry is reflected into the
# recipient_not_found message, so the list length and per-entry length are
# capped BEFORE any routing/reflection happens (LOW-4, 2026-08-15 audit).
_TARGET_SPEC_MAX_ITEMS = 64
_TARGET_SPEC_MAX_LEN = 128

# Receipts echo each requested entry_id back verbatim in not_found entries;
# a 1 MB entry_id was reflected as a 1 MB response body (LOW-4). Bounded at
# the same validation boundary that already refuses non-string entries.
_ENTRY_ID_MAX_LEN = 512


def _validate_room_id(room_id: Any) -> str:
    """Validate a caller-supplied ``room_id`` is a non-empty string.

    A non-string container (list/dict) previously reached the SQLite bind in
    the room resolution queries and raised ``sqlite3.ProgrammingError``, which
    escaped as an HTTP 500 / MCP internal_error. Only the TYPE is checked
    here — a well-formed but fabricated id still resolves to the uniform
    ``room_not_found`` 404, so the no-oracle behaviour is unchanged.
    """
    if not isinstance(room_id, str) or not room_id.strip():
        raise RoomError("invalid_argument", "room_id must be a non-empty string", 400)
    return room_id


def _normalize_target_spec(target_spec: Any) -> list[str]:
    """Normalize a send's ``target_spec`` into a bounded list of strings.

    ``target_spec`` must be a string or a list/tuple of strings, with at most
    ``_TARGET_SPEC_MAX_ITEMS`` non-empty entries of at most
    ``_TARGET_SPEC_MAX_LEN`` characters each. Anything else (int/bool/float
    previously raised ``TypeError`` inside ``list(...)`` -> 500; dicts were
    silently type-confused into their keys) is the caller's
    ``invalid_argument`` 400. An empty list previously slipped through to
    routing and produced a ``200`` with zero receipts — a silent no-op send
    indistinguishable from success — so it is rejected here too, before
    routing ever runs. The bounds keep the ``recipient_not_found`` reflection
    small (LOW-4).
    """
    if isinstance(target_spec, str):
        specs = [target_spec]
    elif isinstance(target_spec, (list, tuple)):
        specs = list(target_spec)
    else:
        raise RoomError(
            "invalid_argument", "target_spec must be a string or a list of strings", 400,
        )
    if not specs:
        raise RoomError("invalid_argument", "target_spec must contain non-empty strings", 400)
    if len(specs) > _TARGET_SPEC_MAX_ITEMS:
        raise RoomError(
            "invalid_argument",
            f"target_spec must contain at most {_TARGET_SPEC_MAX_ITEMS} entries",
            400,
        )
    for spec in specs:
        if not isinstance(spec, str) or not spec.strip() or len(spec) > _TARGET_SPEC_MAX_LEN:
            raise RoomError(
                "invalid_argument",
                f"target_spec must contain non-empty strings of at most "
                f"{_TARGET_SPEC_MAX_LEN} characters",
                400,
            )
    return specs


def _validate_room_name(value: Any) -> str | None:
    """Validate optional room name before any quota/counter mutation."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise RoomError("invalid_argument", "name must be an optional string")
    stripped = value.strip()
    if not stripped:
        raise RoomError("invalid_argument", "name must not be empty when provided")
    if len(stripped) > _ROOM_NAME_MAX_LENGTH:
        raise RoomError(
            "invalid_argument",
            f"name must be at most {_ROOM_NAME_MAX_LENGTH} characters",
        )
    return stripped


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Persisted JSON is invalid")


def _idempotent_receipts(tx: Any, tenant_id: str, room_id: str,
                         sender_agent_id: str, event_row: Any) -> list[dict]:
    """Return delivery and consumption state for a persisted send.

    Receipt rows are keyed by the event identity, not reconstructed from the
    serialized payload. That makes retries stable even when payload encoding
    or an outbox implementation changes, while preserving the existing
    ``status`` field as delivery state and adding ``read_status`` for the
    recipient's consumption state.
    """
    rows = tx.execute(
        "SELECT r.recipient_agent_id, r.entry_id, r.read_status, o.status "
        "FROM cloud_room_receipts r "
        "LEFT JOIN cloud_outbox o ON o.tenant_id = r.tenant_id "
        "AND o.entry_id = r.entry_id "
        "WHERE r.tenant_id = ? AND r.room_id = ? AND r.seq = ? "
        "AND r.sender_agent_id = ? ORDER BY r.recipient_agent_id, r.entry_id",
        (tenant_id, room_id, int(event_row["seq"]), sender_agent_id),
    ).fetchall()
    return [{
        "agent_id": row["recipient_agent_id"],
        "entry_id": row["entry_id"],
        "status": row["status"] or "unknown",
        "read_status": row["read_status"],
    } for row in rows]


def _check_idempotency_conflict(event_row: Any, payload: Any,
                                target_spec: Any, exclude_sender: bool,
                                message_kind: str | None) -> None:
    """Reject reuse of a key for a materially different send request."""
    event_payload = _parse_json(event_row["payload_json"], {})
    if not isinstance(event_payload, dict):
        raise RoomError("idempotency_conflict", "Idempotency key is already in use", 409)
    if (
        event_payload.get("payload") != payload
        or event_payload.get("target_spec") != target_spec
        or event_payload.get("exclude_sender", True) != exclude_sender
        or event_row["message_kind"] != message_kind
    ):
        raise RoomError(
            "idempotency_conflict",
            "Idempotency key was already used with different send parameters",
            409,
        )


def _append_event_tx(tx: Any, tenant_id: str, room_id: str, origin: str,
                     kind: str, payload: Any) -> int:
    """Append a lifecycle event using the caller's open transaction."""
    row = tx.execute(
        "SELECT cursor_head FROM cloud_rooms WHERE tenant_id = ? AND room_id = ?",
        (tenant_id, room_id),
    ).fetchone()
    seq = int(row["cursor_head"]) + 1
    event_id = _new_id("revt")
    tx.execute(
        "INSERT INTO cloud_room_event_log("
        " event_id, room_id, tenant_id, seq, origin_agent, kind, message_kind,"
        " payload_json, idempotency_key, trace_id, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (event_id, room_id, tenant_id, seq, origin, kind, None,
         _json(payload), f"{room_id}:{kind}:{_new_id('idem')}", utc_now_iso()),
    )
    tx.execute(
        "UPDATE cloud_rooms SET cursor_head = ? WHERE tenant_id = ? AND room_id = ?",
        (seq, tenant_id, room_id),
    )
    return seq


def offboard_account_memberships_in_tx(tx: Any, tenant_id: str,
                                       account_id: str) -> int:
    """Mark an account's active account/key room seats as left atomically.

    The account's KEY identities are looked up under ``tenant_id`` — the org
    being left — because a key belongs to exactly one (tenant, account) and
    only THIS org's keys die with the membership. The account's MEMBERSHIPS,
    however, are matched on identity alone: ``account_id`` (``acct_``) and
    ``key_id`` (``key_``) are globally unique random identifiers, and a
    cross-tenant join stores the membership row under the ROOM's tenant — the
    link IS the authorization — so scoping the membership query to ``tenant_id``
    would find zero rows for any seat the account held in another tenant's room.
    Releasing across that boundary is correct ONLY here because this identity is
    being permanently removed from the org. Downstream writes (membership
    status, group rows, cursors, member counter, event log) are keyed on each
    row's own ``tenant_id`` — the room's tenant — never on ``tenant_id``.
    """
    key_rows = tx.execute(
        "SELECT key_id FROM cloud_identity_agent_keys "
        "WHERE tenant_id = ? AND account_id = ?",
        (tenant_id, account_id),
    ).fetchall()
    identities = [account_id, *[row["key_id"] for row in key_rows]]
    placeholders = ",".join("?" for _ in identities)
    rows = tx.execute(
        "SELECT tenant_id, room_id, agent_id FROM cloud_room_members "
        "WHERE status = 'active' AND agent_id IN (" + placeholders + ")",
        (*identities,),
    ).fetchall()
    for row in rows:
        room_tenant_id = row["tenant_id"]
        tx.execute(
            "UPDATE cloud_room_members SET status = 'left' "
            "WHERE tenant_id = ? AND room_id = ? AND agent_id = ? AND status = 'active'",
            (room_tenant_id, row["room_id"], row["agent_id"]),
        )
        tx.execute(
            "DELETE FROM cloud_room_group_members "
            "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
            (room_tenant_id, row["room_id"], row["agent_id"]),
        )
        tx.execute(
            "DELETE FROM cloud_room_cursors "
            "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
            (room_tenant_id, row["room_id"], row["agent_id"]),
        )
        tx.execute(
            "UPDATE cloud_room_counters SET value = MAX(0, value - 1), updated_at = ? "
            "WHERE tenant_id = ? AND room_id = ? AND counter = 'members'",
            (utc_now_iso(), room_tenant_id, row["room_id"]),
        )
        _append_event_tx(
            tx, room_tenant_id, row["room_id"], row["agent_id"], "room.left",
            {"agent_id": row["agent_id"], "reason": "org_member_removed"},
        )
    return len(rows)


def close_agent_key_owned_rooms_in_tx(tx: Any, key_id: str) -> int:
    """Close every open room whose owner identity is a revoked agent key."""
    exists = tx.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_rooms'"
    ).fetchone()
    if exists is None:
        return 0
    rows = tx.execute(
        "SELECT tenant_id, room_id FROM cloud_rooms "
        "WHERE owner_agent_id = ? AND state != 'closed'",
        (key_id,),
    ).fetchall()
    for row in rows:
        room_tenant_id = row["tenant_id"]
        room_id = row["room_id"]
        tx.execute(
            "UPDATE cloud_rooms SET state = 'closed' "
            "WHERE tenant_id = ? AND room_id = ? AND state != 'closed'",
            (room_tenant_id, room_id),
        )
        tx.execute(
            "UPDATE cloud_room_links SET revoked = 1 "
            "WHERE tenant_id = ? AND room_id = ?",
            (room_tenant_id, room_id),
        )
        tx.execute(
            "UPDATE cloud_counters SET value = MAX(0, value - 1), updated_at = ? "
            "WHERE tenant_id = ? AND counter = 'rooms'",
            (utc_now_iso(), room_tenant_id),
        )
        _append_event_tx(
            tx, room_tenant_id, room_id, key_id, "room.closed",
            {"room_id": room_id, "reason": "agent_key_revoked"},
        )
    return len(rows)


def release_agent_key_seats_in_tx(tx: Any, tenant_id: str, key_id: str) -> int:
    """Release every active room seat held by ONE agent-key identity.

    Called from key revocation IN THE SAME transaction as the revoke, so a
    crash can never leave a revoked key's memberships unreachable — the
    credential that could have called ``leave`` dies the moment the revoke
    commits. This is the fix for "revoking a key permanently burns a seat":
    a compromised key's owner is no longer rewarded with a degraded room.

    The membership row is kept with ``status='left'`` — never deleted — so
    past events stay readable and correctly attributed to the key identity
    (the same contract as ``offboard_account_memberships_in_tx``). ``left``,
    not ``stale``, is the right status: ``stale`` is a PRESENCE notion that
    still counts toward the cap, while ``left`` is the seat-releasing status
    that rejoin restores — and a revoked key can never rejoin, so the freed
    seat is exactly the one a new identity should be able to take.

    Scoped by ``key_id`` ALONE — NOT by ``(tenant_id, key_id)``. A ``key_id``
    is a globally unique random identifier (``key_`` + hex), and a key member's
    ``agent_id`` IS that ``key_id``, so matching on the identity alone touches
    exactly this key's memberships and nothing else (account members use the
    disjoint ``acct_`` prefix). The membership rows live under the tenant of
    the ROOM the key joined, which may differ from ``tenant_id`` (the key's
    OWNING tenant): a cross-tenant join stores the row under the room's tenant
    by design — the link IS the authorization. Releasing seats across that
    boundary is correct ONLY here because the caller has just permanently
    destroyed this identity (see ``agent_keys.revoke``: the release runs only
    after the revoke UPDATE actually matched ``(key_id, tenant_id, account_id)``),
    so any seat this key held anywhere is legitimately freed. Downstream writes
    (membership status, member counter, event log) are keyed on each row's own
    ``tenant_id`` — the room's tenant — never on ``tenant_id``.

    The per-room member counter is decremented (clamped at zero) exactly as
    ``leave_room`` does, so the plan-level member quota stays in step with the
    ACTIVE membership set.
    """
    # If this key owned a room, close it before releasing the key's membership
    # seats. Otherwise the room would remain open with a dead owner identity,
    # its link usable, and its tenant room quota permanently occupied.
    close_agent_key_owned_rooms_in_tx(tx, key_id)

    # The room plane may not be initialized on a bare identity backend
    # (revoke is a valid identity operation with no rooms). A missing table
    # simply means there are no seats to release.
    exists = tx.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_room_members'"
    ).fetchone()
    if exists is None:
        return 0
    rows = tx.execute(
        "SELECT tenant_id, room_id FROM cloud_room_members "
        "WHERE agent_id = ? AND status = 'active'",
        (key_id,),
    ).fetchall()
    for row in rows:
        room_tenant_id = row["tenant_id"]
        tx.execute(
            "UPDATE cloud_room_members SET status = 'left' "
            "WHERE tenant_id = ? AND room_id = ? AND agent_id = ? AND status = 'active'",
            (room_tenant_id, row["room_id"], key_id),
        )
        tx.execute(
            "UPDATE cloud_room_counters SET value = MAX(0, value - 1), updated_at = ? "
            "WHERE tenant_id = ? AND room_id = ? AND counter = 'members'",
            (utc_now_iso(), room_tenant_id, row["room_id"]),
        )
        _append_event_tx(
            tx, room_tenant_id, row["room_id"], key_id, "room.left",
            {"agent_id": key_id, "reason": "agent_key_revoked"},
        )
    return len(rows)


class RoomError(Exception):
    """Carries a machine-readable error code for the HTTP surface."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Schema — room_-prefixed, additive, no existing table modified.
# The cloud room tables mirror the coordinator's room_ tables but live in the
# cloud database and carry tenant_id for structural isolation.
# ---------------------------------------------------------------------------

_ROOM_SCHEMA_SQL = """
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
    resume_marker_seq INTEGER,
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


class CloudRoomService:
    """Room lifecycle + addressing over the cloud storage backend.

    Every method takes ``tenant_id`` as a required positional parameter. The
    backend scopes every query by ``WHERE tenant_id = ?``. A caller that omits
    the tenant raises ``TypeError``; a wrong tenant is rejected by the storage
    layer before any room logic runs.
    """

    def __init__(self, backend: StorageBackend) -> None:
        self.backend = backend
        # SQLite serializes writers, but the rate window is checked in a
        # separate transaction from the event append. Serialize keyed sends
        # in-process so two same-key retries cannot both consume the budget
        # before one discovers the other's committed event.
        self._idempotency_lock = threading.Lock()
        self._ensure_room_schema()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def _ensure_room_schema(self) -> None:
        """Idempotent room-schema bootstrap against the cloud backend.

        Runs on every construction. The base schema SQL is idempotent
        (CREATE TABLE/INDEX IF NOT EXISTS); the ``message_kind`` column is
        added to an existing event-log table conditionally, so databases
        created before the column existed are upgraded in place.
        """
        with self.backend.transaction() as tx:
            tx.executescript(_ROOM_SCHEMA_SQL)
            self._ensure_message_kind_column(tx)
            self._ensure_resume_marker_column(tx)
            tx.commit()

    def _ensure_message_kind_column(self, tx: Any) -> None:
        """Add the ``message_kind`` column to an existing event log if missing."""
        row = tx.execute(
            "SELECT 1 FROM pragma_table_info('cloud_room_event_log') WHERE name = 'message_kind'"
        ).fetchone()
        if row is None:
            tx.execute("ALTER TABLE cloud_room_event_log ADD COLUMN message_kind TEXT")

    def _ensure_resume_marker_column(self, tx: Any) -> None:
        """Add the durable resume marker to pre-marker cursor tables."""
        row = tx.execute(
            "SELECT 1 FROM pragma_table_info('cloud_room_cursors') WHERE name = 'resume_marker_seq'"
        ).fetchone()
        if row is None:
            tx.execute("ALTER TABLE cloud_room_cursors ADD COLUMN resume_marker_seq INTEGER")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_room(self, tx: Any, tenant_id: str, room_id: str) -> Any:
        row = tx.execute(
            "SELECT * FROM cloud_rooms WHERE tenant_id = ? AND room_id = ?",
            (tenant_id, room_id),
        ).fetchone()
        if row is None:
            raise RoomError("room_not_found", "Room not found", 404)
        return row

    def _close_expired_room(self, tenant_id: str, room_id: str) -> None:
        """Lazy TTL close, committed in its OWN short transaction.

        ``ttl_seconds`` is a room-lifetime promise, not a link-only limit.
        Reads stay available on closed rooms (history is immutable, like
        explicitly-closed rooms today), but the first write after expiry
        marks the room closed exactly once — with a ``room.closed`` event
        carrying ``reason: ttl_expired`` — so the caller's own transaction
        then sees ``closed`` and refuses. The close must COMMIT independently:
        raising inside the caller's transaction would roll the close back with
        the refusal, which is how the first implementation failed its test.
        """
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            if room["state"] == "closed":
                tx.commit()
                return
            expires = float(room["expires_at"] or 0.0)
            if expires <= 0 or expires >= _time.time():
                tx.commit()
                return
            tx.execute(
                "UPDATE cloud_rooms SET state = 'closed' WHERE tenant_id = ? AND room_id = ?",
                (tenant_id, room_id),
            )
            # Mirror close_room: closing releases the tenant's active-room
            # quota slot. Clamped at zero so a drifted counter can never go
            # negative.
            tx.execute(
                "UPDATE cloud_counters SET value = MAX(0, value - 1), updated_at = ? "
                "WHERE tenant_id = ? AND counter = 'rooms'",
                (utc_now_iso(), tenant_id),
            )
            self._append_event(tx, tenant_id, room_id, room["owner_agent_id"],
                               "room.closed", {"reason": "ttl_expired"})
            tx.commit()

    def _resolve_room_tenant(self, tx: Any, room_id: str, agent_id: str | None = None) -> str:
        """Find the tenant_id for a room, optionally scoped to a member.

        Used when the caller's session tenant may differ from the room's
        owning tenant (cross-tenant join via link). If agent_id is given,
        only returns the tenant if the agent is an active member.

        Membership is bound to the ACCOUNT — ``agent_id`` is the account id,
        established by the authenticated session layer, never by the request
        body. Resolving by the account alone is what makes membership survive
        a re-login (the session token rotates on every sign-in while the
        account never changes). A caller who is not an active member of the
        room resolves the same uniform ``room_not_found`` as a room that never
        existed (no existence oracle).
        """
        _validate_room_id(room_id)
        if agent_id:
            row = tx.execute(
                "SELECT tenant_id FROM cloud_room_members "
                "WHERE room_id = ? AND agent_id = ? AND status = 'active'",
                (room_id, agent_id),
            ).fetchone()
            if row is None:
                raise RoomError("room_not_found", "Room not found", 404)
            return row["tenant_id"]
        row = tx.execute(
            "SELECT tenant_id FROM cloud_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        if row is None:
            raise RoomError("room_not_found", "Room not found", 404)
        return row["tenant_id"]

    def _require_member(self, tx: Any, tenant_id: str, room_id: str, agent_id: str) -> Any:
        row = tx.execute(
            "SELECT * FROM cloud_room_members WHERE tenant_id = ? AND room_id = ? AND agent_id = ? AND status = 'active'",
            (tenant_id, room_id, agent_id),
        ).fetchone()
        if row is None:
            raise RoomError("member_required", "Only room members can access this room", 403)
        return row

    def _touch_member(self, tx: Any, tenant_id: str, room_id: str, agent_id: str,
                      now: float | None = None) -> None:
        """Throttled presence refresh for the CALLER'S OWN membership row.

        Liveness describes whether a member IS USING the room, not whether they
        called one specific bookkeeping tool - so every authenticated room call
        refreshes last_seen. The write is bounded to at most one per
        ``ROOM_LIVENESS_TOUCH_INTERVAL`` (readers do not serialize the room
        behind SQLite's single writer). Only ever touches the row keyed by
        ``agent_id`` - the caller's authenticated account, never a request
        argument - and a missing row is a no-op, so a non-member call can
        neither create nor touch a membership row. Callers invoke this AFTER
        ``_require_member``, so auth is established before any write.

        Performance contract: the throttle check happens IN MEMORY first, so
        the common case does ZERO database work (the same optimization that
        fixed the coordinator envelope perf-gate regression). The memo is a
        throttle only, never an authorization or correctness input.
        """
        if now is None:
            now = _time.time()
        memo = getattr(self, "_touch_memo", None)
        if memo is None:
            memo = {}
            self._touch_memo = memo
        key = (tenant_id, room_id, agent_id)
        last = memo.get(key)
        if last is not None and now - last < ROOM_LIVENESS_TOUCH_INTERVAL:
            return
        memo[key] = now
        row = tx.execute(
            "SELECT last_seen FROM cloud_room_members "
            "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
            (tenant_id, room_id, agent_id),
        ).fetchone()
        if row is None:
            return
        if now - float(row["last_seen"]) < ROOM_LIVENESS_TOUCH_INTERVAL:
            return
        tx.execute(
            "UPDATE cloud_room_members SET last_seen = ?, status = 'active' "
            "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
            (now, tenant_id, room_id, agent_id),
        )

    @staticmethod
    def _filter_payload_for_agent(payload: Any, agent_id: str,
                                  origin_agent: str | None = None) -> dict:
        """Redact message payloads not addressed to ``agent_id``.

        Callers use this only for ``room.message`` events. Only agents listed
        in ``targets`` (or the originator, or everyone for broadcast
        ``target_spec == "*"``) may see the payload. Non-addressees receive a
        redacted envelope so the ordered event sequence stays visible without
        leaking the body.
        """
        # Valid room.message envelopes are objects. A malformed or legacy row
        # must fail closed rather than leaking a scalar to every member.
        if not isinstance(payload, dict):
            return {"redacted": True, "reason": "not_the_addressee"}
        if payload.get("target_spec") == "*" or origin_agent == agent_id:
            return payload
        targets = payload.get("targets")
        if isinstance(targets, list) and agent_id in targets:
            return payload
        return {"redacted": True, "reason": "not_the_addressee"}

    def _append_event(self, tx: Any, tenant_id: str, room_id: str, origin: str,
                      kind: str, payload: Any, idempotency_key: str | None = None,
                      message_kind: str | None = None) -> int:
        row = tx.execute(
            "SELECT cursor_head FROM cloud_rooms WHERE tenant_id = ? AND room_id = ?",
            (tenant_id, room_id),
        ).fetchone()
        seq = int(row["cursor_head"]) + 1
        event_id = _new_id("revt")
        if idempotency_key is None:
            idempotency_key = f"{room_id}:{kind}:{_new_id('idem')}"
        tx.execute(
            "INSERT INTO cloud_room_event_log("
            " event_id, room_id, tenant_id, seq, origin_agent, kind, message_kind,"
            " payload_json, idempotency_key, trace_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (event_id, room_id, tenant_id, seq, origin, kind, message_kind,
             _json(payload), idempotency_key, utc_now_iso()),
        )
        tx.execute(
            "UPDATE cloud_rooms SET cursor_head = ? WHERE tenant_id = ? AND room_id = ?",
            (seq, tenant_id, room_id),
        )
        return seq

    def _route_targets(self, tx: Any, tenant_id: str, room_id: str, target_spec: Any) -> list[str]:
        """Expand a target spec over the room's own member set.

        Deliverability is keyed on MEMBERSHIP, not on current presence.
        ``stale`` is a derived presence notion (idle longer than the presence
        window); an idle member still holds a seat, is still listed on the
        roster, and must still receive its mail — the event log IS the delivery
        mechanism and the recipient reads it when it returns. Only ``left``
        members and non-members are unroutable.

        This is the P0 fix: routing previously dropped idle members, so a
        unicast to one created NO delivery row and NO receipt, and the poll
        redaction check then never counted that member as an addressee — the
        message was PERMANENTLY lost to its own intended recipient, with the
        sender told nothing.
        """
        member_rows = tx.execute(
            "SELECT agent_id FROM cloud_room_members "
            "WHERE tenant_id = ? AND room_id = ? AND status = 'active'",
            (tenant_id, room_id),
        ).fetchall()
        active_ids: set[str] = {row["agent_id"] for row in member_rows}

        group_rows = tx.execute(
            "SELECT group_name, agent_id FROM cloud_room_group_members "
            "WHERE tenant_id = ? AND room_id = ?",
            (tenant_id, room_id),
        ).fetchall()
        groups: dict[str, set[str]] = {}
        for row in group_rows:
            groups.setdefault(row["group_name"], set()).add(row["agent_id"])

        def _expand(spec: str) -> set[str]:
            if spec == "*":
                return set(active_ids)
            if spec in active_ids:
                # Agent identities are authoritative. A member may create a
                # group with the same name as another agent, but that must not
                # turn an exact unicast into a group send.
                return {spec}
            if spec in groups:
                return groups[spec] & active_ids
            return set()

        specs = _normalize_target_spec(target_spec)

        result: set[str] = set()
        for spec in specs:
            result |= _expand(spec)
        return sorted(result)

    def _reject_unroutable_specs(self, tx: Any, tenant_id: str, room_id: str,
                                 target_spec: Any, routed: list[str]) -> None:
        """Refuse a send that names a target that is not a current member.

        NO SILENT SUCCESS: ``200`` with ``receipts: []`` was indistinguishable
        from a successful send. A spec that names an agent (a bare string that
        is neither ``"*"`` nor a group name) must resolve to a current member
        or the whole send is refused with ``recipient_not_found`` — the caller
        can fix its own call from the error alone. ``left`` members and
        never-joined ids both hit this: they are genuinely undeliverable.
        The caller is a member, so naming a non-member of THIS room leaks
        nothing the caller could not already read from the room's roster.
        """
        specs = _normalize_target_spec(target_spec)
        group_rows = tx.execute(
            "SELECT group_name FROM cloud_room_groups WHERE tenant_id = ? AND room_id = ?",
            (tenant_id, room_id),
        ).fetchall()
        group_names = {row["group_name"] for row in group_rows}
        unrouted = [spec for spec in specs
                    if isinstance(spec, str) and spec != "*"
                    and spec not in routed and spec not in group_names]
        if unrouted:
            raise RoomError(
                "recipient_not_found",
                f"Recipient(s) are not members of this room: {', '.join(unrouted)}",
                422,
            )

    # ------------------------------------------------------------------
    # Room lifecycle
    # ------------------------------------------------------------------

    def create_room(self, tenant_id: str, owner_agent_id: str, actor_token: str,
                    cap: int = 10, name: str | None = None, ttl_seconds: int = 86400,
                    origin: str | None = None,
                    actor_account_id: str | None = None) -> dict:
        """Create a room and return its shareable link.

        The owner auto-joins as the first active member. ``owner_agent_id``
        is the caller's ACCOUNT — the identity that owns the room and every
        authorization that follows. ``actor_token`` is recorded only as an
        informational SHA-256 hash (the session that minted the membership);
        it is never consulted to authorize a later room operation, so a
        re-login cannot revoke the owner's seat.

        ``shareable_link`` is an absolute, self-describing URL
        (``{origin}/j/{link_token}``) so an agent that receives ONLY the link
        can fetch it to discover the join endpoint and protocol. ``link_token``
        remains in the response unchanged — it is what the join API consumes.
        """
        if not isinstance(tenant_id, str) or not tenant_id:
            raise RoomError("invalid_argument", "tenant_id is required")
        if not isinstance(owner_agent_id, str) or not owner_agent_id:
            raise RoomError("invalid_argument", "owner_agent_id is required")
        if not isinstance(actor_token, str) or len(actor_token) < 16:
            raise RoomError("actor_auth_invalid", "Actor token is invalid", 401)
        if not isinstance(cap, int) or cap < 2:
            raise RoomError("invalid_argument", "cap must be an integer >= 2")
        if not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise RoomError("invalid_argument", "ttl_seconds must be a positive integer")
        name = _validate_room_name(name)
        # Plan-aware cap bound: a room may never ask for more members than its
        # plan allows. PLANS is the single source of truth for the limit.
        validate_room_cap(self.backend, tenant_id, cap)

        actor_token_hash = _token_hash(actor_token)
        room_id = _new_id("room")
        link_id = _new_id("link")
        raw_token = f"rm_{secrets.token_urlsafe(32)}"
        now = utc_now_iso()
        now_epoch = _time.time()
        expires_at = now_epoch + ttl_seconds
        base_origin = public_origin(origin)

        # ONE transaction for full room creation. Revalidate the live bearer
        # credential and tenant role on this same writer transaction before any
        # room/quota/member/event mutation, closing the request/leave gap.
        plan_id, plan = resolve_plan(self.backend, tenant_id)
        with self.backend.transaction() as tx:
            if actor_account_id is not None:
                credential = tx.execute(
                    "SELECT account_id FROM cloud_identity_sessions "
                    "WHERE token_hash = ? AND tenant_id = ? AND account_id = ? "
                    "AND revoked_at IS NULL AND expires_at > ?",
                    (_token_hash(actor_token), tenant_id, actor_account_id, _time.time()),
                ).fetchone()
                if credential is None:
                    credential = tx.execute(
                        "SELECT account_id FROM cloud_identity_agent_keys "
                        "WHERE token_hash = ? AND tenant_id = ? AND account_id = ? "
                        "AND key_id = ? AND revoked_at IS NULL",
                        (_token_hash(actor_token), tenant_id, actor_account_id, owner_agent_id),
                    ).fetchone()
                if credential is None:
                    raise AuthError("invalid_session")
                require_db_role_in_tx(tx, tenant_id, actor_account_id, "admin")
            bind_room_with_quota_in_tx(
                tx, tenant_id, room_id, self.backend.state_path, plan_id, plan,
            )
            increment_room_member_counter(tx, tenant_id, room_id, plan_id, plan)
            tx.execute(
                "INSERT INTO cloud_rooms("
                " room_id, tenant_id, owner_agent_id, name, cap, state, link_id, created_at, expires_at, cursor_head"
                ") VALUES (?, ?, ?, ?, ?, 'forming', ?, ?, ?, 0)",
                (room_id, tenant_id, owner_agent_id, name, cap, link_id, now, expires_at),
            )
            tx.execute(
                "INSERT INTO cloud_room_links(link_id, room_id, tenant_id, token_hash, created_by, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (link_id, room_id, tenant_id, _token_hash(raw_token), owner_agent_id, now, expires_at),
            )
            # Owner auto-joins.
            tx.execute(
                "INSERT INTO cloud_room_members("
                " tenant_id, room_id, agent_id, joined_at, last_seen, status, capabilities_json, actor_token_hash"
                ") VALUES (?, ?, ?, ?, ?, 'active', '[]', ?)",
                (tenant_id, room_id, owner_agent_id, now, now_epoch, actor_token_hash),
            )
            tx.execute(
                "INSERT INTO cloud_room_cursors(tenant_id, room_id, agent_id, last_ack_seq, updated_at) VALUES (?, ?, ?, 0, ?)",
                (tenant_id, room_id, owner_agent_id, now),
            )
            self._append_event(tx, tenant_id, room_id, owner_agent_id, "room.created",
                               {"room_id": room_id, "owner_agent_id": owner_agent_id, "cap": cap})
            self._append_event(tx, tenant_id, room_id, owner_agent_id, "room.joined",
                               {"agent_id": owner_agent_id, "status": "active"})
            tx.commit()

        return {
            "room_id": room_id,
            "link_id": link_id,
            "link_token": raw_token,
            "shareable_link": f"{base_origin}/j/{raw_token}",
            "expires_at": expires_at,
            "cap": cap,
            "state": "forming",
            "owner_agent_id": owner_agent_id,
        }

    def _resolve_room_for_link(self, tx: Any, room_id: str, link_token: str) -> tuple[str, Any, Any]:
        """Resolve the room and tenant for a join-by-link call.

        The link is the cross-tenant capability: a holder of a valid link can
        join the room regardless of which tenant their session belongs to. We
        look up the room by (room_id, token_hash) without a tenant filter, then
        return the room's OWN tenant_id so all subsequent queries are correctly
        scoped. An invalid link yields invalid_link (no oracle on which rooms
        exist).
        """
        _validate_room_id(room_id)
        try:
            link_hash = _token_hash(link_token)
        except ValueError:
            raise RoomError("invalid_link", "Link is not valid for this room", 403)
        link_row = tx.execute(
            "SELECT * FROM cloud_room_links WHERE room_id = ? AND token_hash = ?",
            (room_id, link_hash),
        ).fetchone()
        if link_row is None:
            raise RoomError("invalid_link", "Link is not valid for this room", 403)
        real_tenant_id = link_row["tenant_id"]
        room = self._require_room(tx, real_tenant_id, room_id)
        return real_tenant_id, room, link_row

    def resolve_room_by_link_token(self, link_token: str) -> str | None:
        """Return the room_id a valid link token opens, or None if unknown.

        PURE READ — it never joins the room, never consumes or invalidates the
        token, and returns nothing about orgs, members, or the event log. It is
        what the unauthenticated ``GET /j/<token>`` descriptor endpoint uses to
        answer "which room does this link open?" for a joining agent.
        Malformed and unknown tokens both resolve to ``None`` so the endpoint
        cannot be used as an oracle.
        """
        try:
            token_hash = _token_hash(link_token)
        except ValueError:
            return None
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT room_id FROM cloud_room_links WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return row["room_id"] if row is not None else None

    def join_room(self, tenant_id: str, room_id: str, link_token: str, agent_id: str,
                  consent: Any, actor_token: str, capabilities: list[str] | None = None) -> dict:
        """Redeem a multi-use link to join a room.

        ``consent`` must be the literal JSON boolean ``true``. The link is
        multi-use up to the cap — it is NOT consumed. ``agent_id`` is the
        caller's ACCOUNT (the authenticated session's account id); re-join
        with the same account is idempotent and refreshes presence, so a
        member who signs in again keeps their seat with a fresh session.

        The link is a cross-tenant capability: the room is resolved by the
        link's token_hash, and the room's OWN tenant_id is used for all
        subsequent queries. The caller's session tenant_id is only used as a
        fallback when the link itself does not resolve (and fails).
        """
        if not isinstance(agent_id, str) or not agent_id:
            raise RoomError("invalid_argument", "agent_id is required")
        if consent is not True:
            raise RoomError("consent_required", "consent must be the literal JSON boolean true", 400)
        if not isinstance(actor_token, str) or len(actor_token) < 16:
            raise RoomError("actor_auth_invalid", "Actor token is invalid", 401)

        actor_token_hash = _token_hash(actor_token)
        caps = list(capabilities) if capabilities else []
        now_epoch = _time.time()

        # ONE transaction for the whole join. Resolve the room by its link,
        # gate the seat (plan member cap + the room's own cap), and record the
        # membership. The counter increment and the membership write commit or
        # roll back together, so a refused join can never burn a member slot
        # and concurrent joins cannot oversubscribe a room.
        with self.backend.transaction() as tx:
            real_tenant_id, room, link_row = self._resolve_room_for_link(tx, room_id, link_token)
            if room["state"] != "closed" and float(room["expires_at"] or 0.0) > 0 \
                    and float(room["expires_at"]) < now_epoch:
                # Close commits BEFORE the refusal raise, so the close is not
                # rolled back with it. The tenant's active-room quota slot is
                # released too (mirror close_room).
                tx.execute(
                    "UPDATE cloud_rooms SET state = 'closed' WHERE tenant_id = ? AND room_id = ?",
                    (real_tenant_id, room_id),
                )
                tx.execute(
                    "UPDATE cloud_counters SET value = MAX(0, value - 1), updated_at = ? "
                    "WHERE tenant_id = ? AND counter = 'rooms'",
                    (utc_now_iso(), real_tenant_id),
                )
                self._append_event(tx, real_tenant_id, room_id, room["owner_agent_id"],
                                   "room.closed", {"reason": "ttl_expired"})
                tx.commit()
                raise RoomError("room_expired", "Room has expired", 410)
            if room["state"] == "closed":
                raise RoomError("room_closed", "Room is closed", 409)
            if link_row["revoked"]:
                raise RoomError("link_revoked", "Link has been revoked", 410)
            if float(link_row["expires_at"]) < now_epoch:
                raise RoomError("link_expired", "Link has expired", 410)
            existing = tx.execute(
                "SELECT * FROM cloud_room_members WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (real_tenant_id, room_id, agent_id),
            ).fetchone()

            if existing is not None and existing["status"] == "active":
                # Existing active membership for THIS account: refresh presence.
                # Identity is the account, so the same human re-joining after a
                # re-login (fresh session token) simply refreshes presence —
                # the credential is never an authorization input here.
                # Idempotent re-joins are NOT re-counted against the plan
                # member cap and never re-gated by the room cap.
                #
                # Rejoin is otherwise a NO-OP: joined_at is a one-time fact and
                # must not be rewritten (an idempotent client retrying a join
                # silently churned the roster: 07:16:01 -> 07:53:18).
                # Capabilities ARE recorded when the caller states them
                # (explicit intent; an idempotent retry re-sends the same list,
                # and an omitted list keeps the stored value), and the
                # actor-token hash stays as the original join recorded.
                # Only last_seen moves, exactly like heartbeat.
                now = utc_now_iso()
                if caps:
                    tx.execute(
                        "UPDATE cloud_room_members SET last_seen = ?, capabilities_json = ? "
                        "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                        (now_epoch, _json(caps), real_tenant_id, room_id, agent_id),
                    )
                else:
                    tx.execute(
                        "UPDATE cloud_room_members SET last_seen = ? "
                        "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                        (now_epoch, real_tenant_id, room_id, agent_id),
                    )
                tx.execute(
                    "INSERT INTO cloud_room_cursors(tenant_id, room_id, agent_id, last_ack_seq, updated_at) "
                    "VALUES (?, ?, ?, 0, ?) "
                    "ON CONFLICT(tenant_id, room_id, agent_id) DO NOTHING",
                    (real_tenant_id, room_id, agent_id, now),
                )
                cursor_row = tx.execute(
                    "SELECT last_ack_seq FROM cloud_room_cursors WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                    (real_tenant_id, room_id, agent_id),
                ).fetchone()
                cursor = int(cursor_row["last_ack_seq"]) if cursor_row else 0
                tx.commit()
                return {"room_id": room_id, "agent_id": agent_id, "status": "active",
                        "joined_at": existing["joined_at"], "cursor": cursor}

            # New identity OR a left member reactivating — both need a seat.
            # The plan member cap check + counter increment run here (they roll
            # back with this transaction if the room's own cap below refuses),
            # and the room cap is enforced before the membership is recorded.
            # BEGIN IMMEDIATE serializes concurrent joins, so exactly one can
            # take the last slot.
            plan_id, plan = resolve_plan(self.backend, real_tenant_id)
            increment_room_member_counter(tx, real_tenant_id, room_id, plan_id, plan)

            active_count = tx.execute(
                "SELECT COUNT(*) AS c FROM cloud_room_members "
                "WHERE tenant_id = ? AND room_id = ? AND status = 'active'",
                (real_tenant_id, room_id),
            ).fetchone()["c"]
            if active_count >= int(room["cap"]):
                raise RoomError("room_full", f"Room is full (cap {room['cap']})", 409)

            now = utc_now_iso()
            if existing is not None:
                # Left member reactivating — restore in place (the vacated seat
                # is re-counted now that it is being taken again).
                tx.execute(
                    "UPDATE cloud_room_members SET status = 'active', last_seen = ?, "
                    "joined_at = ?, capabilities_json = ?, actor_token_hash = ? "
                    "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                    (now_epoch, now, _json(caps), actor_token_hash,
                     real_tenant_id, room_id, agent_id),
                )
            else:
                tx.execute(
                    "INSERT INTO cloud_room_members("
                    " tenant_id, room_id, agent_id, joined_at, last_seen, status, capabilities_json, actor_token_hash"
                    ") VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                    (real_tenant_id, room_id, agent_id, now, now_epoch, _json(caps), actor_token_hash),
                )
            tx.execute(
                "INSERT INTO cloud_room_cursors(tenant_id, room_id, agent_id, last_ack_seq, updated_at) "
                "VALUES (?, ?, ?, 0, ?) "
                "ON CONFLICT(tenant_id, room_id, agent_id) DO NOTHING",
                (real_tenant_id, room_id, agent_id, now),
            )
            tx.execute(
                "UPDATE cloud_rooms SET state = 'active' "
                "WHERE tenant_id = ? AND room_id = ? AND state = 'forming'",
                (real_tenant_id, room_id),
            )
            self._append_event(tx, real_tenant_id, room_id, agent_id, "room.joined",
                               {"agent_id": agent_id, "status": "active"})
            cursor_row = tx.execute(
                "SELECT last_ack_seq FROM cloud_room_cursors WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (real_tenant_id, room_id, agent_id),
            ).fetchone()
            cursor = int(cursor_row["last_ack_seq"]) if cursor_row else 0
            tx.commit()

        return {"room_id": room_id, "agent_id": agent_id, "status": "active",
                "joined_at": now, "cursor": cursor}

    def room_info(self, tenant_id: str, room_id: str, agent_id: str) -> dict:
        """Member-only room summary with roster and presence.

        The ROOM OWNER additionally sees the link control surface: ``link_id``
        (the identifier ``/v1/rooms/revoke_link`` needs) and ``link_revoked``
        (confirmation a revocation landed). Ordinary members who cannot revoke
        never see either. ``link_id`` is a control-plane identifier; the
        ``link_token`` bearer credential is never exposed here.
        """
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, agent_id)
            self._touch_member(tx, tenant_id, room_id, agent_id)
            members = tx.execute(
                "SELECT * FROM cloud_room_members WHERE tenant_id = ? AND room_id = ? AND status = 'active' ORDER BY joined_at",
                (tenant_id, room_id),
            ).fetchall()
            member_list = []
            for m in members:
                age = max(0.0, _time.time() - float(m["last_seen"]))
                member_list.append({
                    "agent_id": m["agent_id"],
                    "status": "stale" if age > ROOM_STALE_AFTER_SECONDS else "active",
                    "capabilities": _parse_json(m["capabilities_json"], []),
                    "last_seen": float(m["last_seen"]),
                    "joined_at": m["joined_at"],
                })
            result = {
                "room_id": room_id,
                "name": room["name"],
                "state": room["state"],
                "cap": room["cap"],
                "member_count": len(member_list),
                "members": member_list,
                "owner_agent_id": room["owner_agent_id"],
            }
            # Owner-only link control surface (2026-08-12 finding: link_id was
            # unobtainable after room_create). A room has exactly one link
            # (created with the room), so exposing it on room_info gives the
            # owner the identifier revoke_link needs WITHOUT a new endpoint and
            # WITHOUT leaking it to members who cannot revoke (least exposure).
            if room["owner_agent_id"] == agent_id:
                link = tx.execute(
                    "SELECT link_id, revoked FROM cloud_room_links "
                    "WHERE tenant_id = ? AND room_id = ? LIMIT 1",
                    (tenant_id, room_id),
                ).fetchone()
                if link is not None:
                    result["link_id"] = link["link_id"]
                    result["link_revoked"] = bool(link["revoked"])
            return result

    def leave_room(self, tenant_id: str, room_id: str, agent_id: str) -> dict:
        with self.backend.transaction() as tx:
            self._require_room(tx, tenant_id, room_id)
            row = tx.execute(
                "SELECT * FROM cloud_room_members WHERE tenant_id = ? AND room_id = ? AND agent_id = ? AND status = 'active'",
                (tenant_id, room_id, agent_id),
            ).fetchone()
            if row is None:
                raise RoomError("member_required", "Only room members can leave", 403)
            tx.execute(
                "UPDATE cloud_room_members SET status = 'left' WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, agent_id),
            )
            # A leaving member frees a seat: the member counter tracks ACTIVE
            # members, so it must come down with the status. Clamped at zero so
            # a drifted counter can never go negative.
            tx.execute(
                "UPDATE cloud_room_counters SET value = MAX(0, value - 1), updated_at = ? "
                "WHERE tenant_id = ? AND room_id = ? AND counter = 'members'",
                (utc_now_iso(), tenant_id, room_id),
            )
            self._append_event(tx, tenant_id, room_id, agent_id, "room.left",
                               {"agent_id": agent_id})
            tx.commit()
        return {"room_id": room_id, "agent_id": agent_id, "status": "left"}

    def remove_member(self, tenant_id: str, room_id: str, owner_agent_id: str,
                      target_agent_id: str) -> dict:
        """Remove one active member from a room, releasing its seat atomically.

        Owner-only. Removal is NOT a ban: a removed member who still holds a
        valid link can rejoin. The membership row flips to ``left`` (history
        and attribution preserved), group membership and the member's cursor
        row are cleaned up, the member counter is decremented, and a
        ``room.left`` event records the removal with ``reason:
        removed_by_owner`` — all in one transaction.
        """
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, owner_agent_id)
            self._touch_member(tx, tenant_id, room_id, owner_agent_id)
            if room["owner_agent_id"] != owner_agent_id:
                raise RoomError("owner_required", "Only the room owner can remove a member", 403)
            if target_agent_id == room["owner_agent_id"]:
                raise RoomError("owner_required", "The room owner cannot be removed", 403)
            if not isinstance(target_agent_id, str) or not target_agent_id.strip():
                raise RoomError("invalid_argument", "member_id must be a non-empty string", 400)
            target = tx.execute(
                "SELECT 1 FROM cloud_room_members "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ? AND status = 'active'",
                (tenant_id, room_id, target_agent_id),
            ).fetchone()
            if target is None:
                raise RoomError("member_not_found", "Member not found in this room", 404)
            tx.execute(
                "UPDATE cloud_room_members SET status = 'left' "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ? AND status = 'active'",
                (tenant_id, room_id, target_agent_id),
            )
            tx.execute(
                "DELETE FROM cloud_room_group_members "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, target_agent_id),
            )
            tx.execute(
                "DELETE FROM cloud_room_cursors "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, target_agent_id),
            )
            tx.execute(
                "UPDATE cloud_room_counters SET value = MAX(0, value - 1), updated_at = ? "
                "WHERE tenant_id = ? AND room_id = ? AND counter = 'members'",
                (utc_now_iso(), tenant_id, room_id),
            )
            self._append_event(
                tx, tenant_id, room_id, owner_agent_id, "room.left",
                {"agent_id": target_agent_id, "reason": "removed_by_owner"},
            )
            tx.commit()
        return {"room_id": room_id, "agent_id": target_agent_id, "status": "left"}

    def close_room(self, tenant_id: str, room_id: str, caller_agent_id: str) -> dict:
        """Close a room (owner only). Invalidates all links.

        Closing releases the room back to the tenant's ACTIVE-room quota: the
        ``rooms`` counter tracks active (non-closed) rooms, so it is
        decremented here. Clamped at zero so a drifted counter can never go
        negative.
        """
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, caller_agent_id)
            if room["owner_agent_id"] != caller_agent_id:
                raise RoomError("owner_required", "Only the room owner can close it", 403)
            tx.execute(
                "UPDATE cloud_rooms SET state = 'closed' WHERE tenant_id = ? AND room_id = ?",
                (tenant_id, room_id),
            )
            tx.execute(
                "UPDATE cloud_room_links SET revoked = 1 WHERE tenant_id = ? AND room_id = ?",
                (tenant_id, room_id),
            )
            tx.execute(
                "UPDATE cloud_counters SET value = MAX(0, value - 1), updated_at = ? "
                "WHERE tenant_id = ? AND counter = 'rooms'",
                (utc_now_iso(), tenant_id),
            )
            self._append_event(tx, tenant_id, room_id, caller_agent_id, "room.closed",
                               {"room_id": room_id})
            tx.commit()
        return {"room_id": room_id, "state": "closed"}

    def revoke_link(self, tenant_id: str, room_id: str, owner_agent_id: str, link_id: str) -> dict:
        """Revoke a specific link (owner only).

        Error-code decision (2026-08-12 finding: revoke reported success while
        revoking nothing). A MALFORMED link_id is the caller's error:
        ``invalid_argument``, refused before any lookup. A WELL-FORMED but
        unknown, already-revoked, or wrong-room/tenant link_id all collapse to
        the SAME ``link_not_found`` 404 — byte-identical for a link that lives
        in someone else's room/tenant and a link that never existed, so this
        endpoint is not a link-id existence oracle (the codebase has had three
        of those; this must not be a fourth). Success requires exactly one row
        to flip: if zero rows changed, nothing was revoked and returning 200
        would manufacture false confidence exactly when an owner is cutting off
        a leaked link.
        """
        _validate_link_id(link_id)
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, owner_agent_id)
            if room["owner_agent_id"] != owner_agent_id:
                raise RoomError("owner_required", "Only the room owner can revoke links", 403)
            link = tx.execute(
                "SELECT link_id, revoked FROM cloud_room_links "
                "WHERE link_id = ? AND tenant_id = ? AND room_id = ?",
                (link_id, tenant_id, room_id),
            ).fetchone()
            if link is None or link["revoked"]:
                raise RoomError("link_not_found", "Link not found for this room", 404)
            cursor = tx.execute(
                "UPDATE cloud_room_links SET revoked = 1 "
                "WHERE link_id = ? AND tenant_id = ? AND room_id = ? AND revoked = 0",
                (link_id, tenant_id, room_id),
            )
            if cursor.rowcount != 1:
                raise RoomError("link_not_found", "Link not found for this room", 404)
            tx.commit()
        return {"link_id": link_id, "revoked": True}

    def regenerate_link(self, tenant_id: str, room_id: str, owner_agent_id: str) -> dict:
        """Replace a room's raw link after the web process lost its cache.

        The database stores only the hash, so regeneration is deliberately an
        owner-only action that returns one fresh raw token to the current web
        process. Updating the single link row atomically invalidates the old
        token without persisting a bearer secret. A revoked, closed, or expired
        link cannot be silently re-enabled by this recovery path.
        """
        now = utc_now_iso()
        now_epoch = _time.time()
        raw_token = f"rm_{secrets.token_urlsafe(32)}"
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, owner_agent_id)
            if room["owner_agent_id"] != owner_agent_id:
                raise RoomError("owner_required", "Only the room owner can regenerate links", 403)
            if room["state"] == "closed":
                raise RoomError("room_closed", "Room is closed", 409)
            link = tx.execute(
                "SELECT link_id, expires_at, revoked FROM cloud_room_links "
                "WHERE tenant_id = ? AND room_id = ? LIMIT 1",
                (tenant_id, room_id),
            ).fetchone()
            if link is None:
                raise RoomError("link_not_found", "Link not found for this room", 404)
            if link["revoked"]:
                raise RoomError("link_revoked", "Link has been permanently revoked", 410)
            if float(link["expires_at"]) < now_epoch:
                raise RoomError("link_expired", "Link has expired", 410)
            cursor = tx.execute(
                "UPDATE cloud_room_links SET token_hash = ?, created_at = ? "
                "WHERE link_id = ? AND tenant_id = ? AND room_id = ? AND revoked = 0",
                (_token_hash(raw_token), now, link["link_id"], tenant_id, room_id),
            )
            if cursor.rowcount != 1:
                raise RoomError("link_not_found", "Link not found for this room", 404)
            self._append_event(
                tx, tenant_id, room_id, owner_agent_id, "room.link_regenerated",
                {"link_id": link["link_id"]},
            )
            tx.commit()
        return {
            "room_id": room_id,
            "link_id": link["link_id"],
            "link_token": raw_token,
            "expires_at": float(link["expires_at"]),
        }

    def list_rooms_for_member(self, tenant_id: str, agent_id: str) -> list[dict]:
        """List all rooms where the agent is an active member."""
        with self.backend.transaction() as tx:
            rows = tx.execute(
                "SELECT r.room_id, r.name, r.state, r.cap, r.owner_agent_id, r.created_at "
                "FROM cloud_rooms r "
                "JOIN cloud_room_members m ON m.room_id = r.room_id AND m.tenant_id = r.tenant_id "
                "WHERE r.tenant_id = ? AND m.agent_id = ? AND m.status = 'active' "
                "ORDER BY r.created_at",
                (tenant_id, agent_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_rooms_for_member_any_tenant(self, agent_id: str) -> list[dict]:
        """List all rooms where the agent is an active member, across all tenants."""
        with self.backend.transaction() as tx:
            rows = tx.execute(
                "SELECT r.room_id, r.tenant_id, r.name, r.state, r.cap, r.owner_agent_id, r.created_at "
                "FROM cloud_rooms r "
                "JOIN cloud_room_members m ON m.room_id = r.room_id AND m.tenant_id = r.tenant_id "
                "WHERE m.agent_id = ? AND m.status = 'active' "
                "ORDER BY r.created_at",
                (agent_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Event log + cursors
    # ------------------------------------------------------------------

    def poll(self, tenant_id: str, room_id: str, agent_id: str,
             after_seq: int | None = None, limit: int = 100,
             message_kinds: list[str] | None = None) -> dict:
        """Return events after ``after_seq`` (default: last_ack_seq).

        Reads are normally exclusive (``seq > after_seq``). The returned
        ``next_seq`` is persisted as a per-member resume marker; while that
        unacknowledged marker is supplied again, it is rechecked inclusively
        so truncated pages and idle-tail reconnects cannot skip its event.

        ``message_kinds`` is an optional list of sender-set message kinds.
        When present, only events whose ``message_kind`` matches one of the
        entries are returned; when absent, behaviour is exactly as before
        (everything is returned). This filters only the ``events`` list —
        ``next_seq`` and ``cursor_head`` are always reported against the FULL
        stream, so a filtering caller can page through matching events with no
        gaps or repeats and can ack ``cursor_head`` to keep its durable cursor
        in exact agreement with an unfiltered view.
        """
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, agent_id)
            self._touch_member(tx, tenant_id, room_id, agent_id)
            cursor_row = tx.execute(
                "SELECT last_ack_seq, resume_marker_seq FROM cloud_room_cursors "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, agent_id),
            ).fetchone()
            last_ack = int(cursor_row["last_ack_seq"]) if cursor_row else 0
            resume_marker = (
                int(cursor_row["resume_marker_seq"])
                if cursor_row is not None and cursor_row["resume_marker_seq"] is not None
                else None
            )
            if after_seq is None:
                after_seq = last_ack
            if isinstance(after_seq, bool) or not isinstance(after_seq, int):
                # A non-integer cursor is the CALLER's error and must come
                # back as a clean 400, not escape as a ValueError -> 500.
                raise RoomError("invalid_argument", "after_seq must be an integer", 400)
            # Cursor guards (F14 / P1): silence must never mean success for
            # READS either. A window beyond the room head used to be silently
            # echoed back as ``next_seq`` — the caller could not tell their
            # cursor was impossible. Refuse it. Negative cursors were silently
            # clamped; refuse those too. ``behind_by`` reports how many events
            # between the caller's last ack and their window they are skipping.
            if after_seq < 0:
                raise RoomError("invalid_cursor", "after_seq cannot be negative", 400)
            if after_seq > int(room["cursor_head"]) + 1:
                raise RoomError(
                    "invalid_cursor",
                    f"after_seq {after_seq} is beyond the next valid cursor {int(room['cursor_head']) + 1}",
                    400,
                )
            behind_by = max(0, after_seq - last_ack)
            # ``next_seq`` is the first sequence after the page. Persist that
            # resume marker per member so a later poll can use it inclusively:
            # this avoids skipping an event that was already present at a
            # truncated page boundary, or that later occupies an idle tail.
            # A normal current-head cursor remains exclusive because it is not
            # the previously returned resume marker.
            resume_marker_catch_up = (
                resume_marker == after_seq
                and after_seq > last_ack
            )
            sequence_operator = ">=" if resume_marker_catch_up else ">"
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise RoomError("invalid_argument", "limit must be an integer", 400)
            limit = max(1, min(limit, 200))
            kind_filter = _validate_message_kinds(message_kinds)
            if kind_filter:
                placeholders = ", ".join("?" for _ in kind_filter)
                rows = tx.execute(
                    "SELECT * FROM cloud_room_event_log WHERE tenant_id = ? AND room_id = ? "
                    "AND seq " + sequence_operator + " ? AND message_kind IN (" + placeholders + ") "
                    "ORDER BY seq ASC LIMIT ?",
                    (tenant_id, room_id, after_seq, *kind_filter, limit),
                ).fetchall()
            else:
                rows = tx.execute(
                    "SELECT * FROM cloud_room_event_log WHERE tenant_id = ? AND room_id = ? AND seq "
                    + sequence_operator + " ? "
                    "ORDER BY seq ASC LIMIT ?",
                    (tenant_id, room_id, after_seq, limit),
                ).fetchall()
            events = []
            next_seq = after_seq
            for r in rows:
                raw_payload = _parse_json(r["payload_json"], {})
                visible_payload = (
                    self._filter_payload_for_agent(raw_payload, agent_id, r["origin_agent"])
                    if r["kind"] == "room.message"
                    else raw_payload
                )
                events.append({
                    "event_id": r["event_id"],
                    "seq": r["seq"],
                    "origin_agent": r["origin_agent"],
                    "kind": r["kind"],
                    "message_kind": r["message_kind"],
                    "payload": visible_payload,
                    "created_at": r["created_at"],
                })
                next_seq = r["seq"] + 1
            # A filtered read scans the full stream even when no matching row
            # is returned (or when fewer than ``limit`` matches remain). Report
            # the last scanned sequence so callers do not repeatedly replay
            # old non-matching events; a future event at the resume marker
            # remains visible because the unacknowledged marker is rechecked
            # inclusively at the page boundary.
            if kind_filter and len(rows) < limit:
                next_seq = max(next_seq, int(room["cursor_head"]))
            if rows or next_seq == int(room["cursor_head"]) + 1:
                tx.execute(
                    "UPDATE cloud_room_cursors SET resume_marker_seq = ?, updated_at = ? "
                    "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                    (next_seq, utc_now_iso(), tenant_id, room_id, agent_id),
                )
            return {
                "room_id": room_id,
                "state": room["state"],
                "events": events,
                "next_seq": next_seq,
                "cursor_head": int(room["cursor_head"]),
                "last_ack_seq": last_ack,
                "behind_by": behind_by,
                "has_more": len(rows) == limit,
            }

    def wait(self, tenant_id: str, room_id: str, agent_id: str,
             after_seq: int | None = None, timeout_seconds: int = 20,
             limit: int = 100, message_kinds: list[str] | None = None,
             _pulse: Callable[[], None] | None = None) -> dict:
        """Blocking long-poll over ``poll``: the continuous-collaboration primitive.

        Returns as soon as at least one event after ``after_seq`` is available
        (including an event at an unacknowledged resume marker);
        otherwise returns an EMPTY poll result at the timeout — a NORMAL
        outcome, not an error, so a caller simply loops again. This is what
        keeps an agent inside its turn: ``wait, react, wait again`` with no
        human in the loop.

        Semantics are identical to ``poll`` — same redaction, ordering, and
        ``next_seq``/cursor reporting; a non-addressee still receives the
        redacted envelope with its sequence position, never the body. ``wait``
        only drives the same read repeatedly, so a blocking read can never
        become a way around confidentiality.

        Lock discipline: each internal poll opens a fresh short transaction
        that is fully closed before the sleep, so no write lock or transaction
        is ever held while waiting. With SQLite's single-writer model, a
        blocked waiter must not freeze other agents in every room — and it
        does not. ``after_seq`` is pinned on the first read (resolving the
        cursor default once), so concurrent ACKs cannot silently move the
        read window mid-wait.

        Liveness: the caller's ``last_seen`` is refreshed when the wait BEGINS
        and again when it RETURNS (throttled, so a 25-second block never ages
        the caller toward stale — the server holds the connection and knows
        the agent is there).
        """
        try:
            timeout_seconds = int(timeout_seconds)
        except (TypeError, ValueError):
            timeout_seconds = 20
        timeout_seconds = max(0, min(timeout_seconds, 30))
        deadline = _time.monotonic() + timeout_seconds

        # BEGIN: prove the caller is alive the instant the block starts. A
        # blocked waiter is provably alive — the server is holding its
        # connection — so the wait must never age the caller toward stale.
        # This runs in its own short transaction that fully closes before the
        # poll loop, so no write lock is ever held while waiting (the lock
        # discipline below is preserved). Auth is established here too, so a
        # non-member is refused before any blocking starts.
        with self.backend.transaction() as tx:
            self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, agent_id)
            self._touch_member(tx, tenant_id, room_id, agent_id)
            tx.commit()

        result = self.poll(tenant_id, room_id, agent_id, after_seq, limit,
                           message_kinds=message_kinds)
        if not result["events"]:
            pinned_after = int(result["next_seq"])
            while _time.monotonic() < deadline:
                if _pulse is not None:
                    _pulse()
                _time.sleep(0.25)
                result = self.poll(tenant_id, room_id, agent_id, pinned_after, limit,
                                   message_kinds=message_kinds)
                if result["events"]:
                    break

        # RETURN: refresh once more before handing control back, so a long
        # block that ended on a throttled poll never returns with an aged
        # last_seen. (The poll loop already keeps the caller fresh throughout;
        # this is the explicit return bookend.)
        with self.backend.transaction() as tx:
            self._touch_member(tx, tenant_id, room_id, agent_id)
            tx.commit()

        result["timed_out"] = not bool(result["events"])
        return result

    def ack(self, tenant_id: str, room_id: str, agent_id: str, seq: int) -> dict:
        """Acknowledge events up to ``seq`` (monotonic)."""
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, agent_id)
            self._touch_member(tx, tenant_id, room_id, agent_id)
            if isinstance(seq, bool) or not isinstance(seq, int):
                raise RoomError("invalid_argument", "seq must be an integer", 400)
            if seq < 0:
                # Symmetric with poll: a negative cursor is refused with
                # invalid_cursor, never silently accepted (MAX semantics used
                # to let a negative ack look like a successful no-op).
                raise RoomError("invalid_cursor", "seq cannot be negative", 400)
            if seq > int(room["cursor_head"]):
                raise RoomError("invalid_cursor", "Cannot acknowledge an event beyond the room head", 400)
            now = utc_now_iso()
            tx.execute(
                "INSERT INTO cloud_room_cursors(tenant_id, room_id, agent_id, last_ack_seq, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, room_id, agent_id) DO UPDATE SET "
                "last_ack_seq = MAX(last_ack_seq, excluded.last_ack_seq), updated_at = excluded.updated_at",
                (tenant_id, room_id, agent_id, seq, now),
            )
            tx.execute(
                "UPDATE cloud_room_cursors SET resume_marker_seq = NULL "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ? "
                "AND resume_marker_seq IS NOT NULL AND resume_marker_seq <= last_ack_seq",
                (tenant_id, room_id, agent_id),
            )
            # Read-receipt lifecycle: acking past an event's seq means the
            # recipient has processed it — its receipt transitions queued ->
            # read. Scoped to THIS member's receipts only; other recipients'
            # rows are never touched.
            tx.execute(
                "UPDATE cloud_room_receipts SET read_status = 'read', updated_at = ? "
                "WHERE tenant_id = ? AND room_id = ? AND recipient_agent_id = ? "
                "AND seq <= ? AND read_status = 'queued'",
                (now, tenant_id, room_id, agent_id, seq),
            )
            read_row = tx.execute(
                "SELECT COUNT(*) AS c FROM cloud_room_receipts "
                "WHERE tenant_id = ? AND room_id = ? AND recipient_agent_id = ? "
                "AND seq <= ? AND read_status = 'read'",
                (tenant_id, room_id, agent_id, seq),
            ).fetchone()
            row = tx.execute(
                "SELECT last_ack_seq FROM cloud_room_cursors WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, agent_id),
            ).fetchone()
            tx.commit()
        return {"room_id": room_id, "agent_id": agent_id,
                "last_ack_seq": int(row["last_ack_seq"]),
                "receipts_read": int(read_row["c"])}

    def receipts(self, tenant_id: str, room_id: str, agent_id: str,
                 entry_ids: list[str]) -> dict:
        """Sender-scoped delivery/read state for room message receipts.

        The send response returned a receipt per recipient with its
        ``entry_id``; this is the query surface for their CURRENT state
        (receipt status: queued/read, plus the durable outbox entry's
        lifecycle). Scoped to receipts whose ``sender_agent_id`` is the caller:
        an arbitrary entry id must not reveal another sender's outbox state, so
        unknown, foreign, and recipient-owned entry ids all share the same
        ``not_found`` result. Membership checks run before any query, keeping
        the cross-tenant / non-member no-oracle boundary.
        """
        if not isinstance(entry_ids, list):
            raise RoomError("invalid_argument", "entry_ids must be a list of strings", 400)
        if len(entry_ids) > 200:
            raise RoomError("invalid_argument", "entry_ids must contain at most 200 items", 400)
        if any(not isinstance(e, str) or not e.strip() for e in entry_ids):
            raise RoomError("invalid_argument", "entry_ids must contain non-empty strings", 400)
        if any(len(entry_id) > _ENTRY_ID_MAX_LEN for entry_id in entry_ids):
            raise RoomError(
                "invalid_argument",
                f"entry_ids must contain strings of at most {_ENTRY_ID_MAX_LEN} characters",
                400,
            )

        with self.backend.transaction() as tx:
            self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, agent_id)
            self._touch_member(tx, tenant_id, room_id, agent_id)
            rows = []
            if entry_ids:
                placeholders = ",".join("?" for _ in entry_ids)
                rows = tx.execute(
                    "SELECT r.entry_id, r.read_status, o.status, o.attempts, "
                    "o.next_attempt_at, o.last_error "
                    "FROM cloud_room_receipts r "
                    "LEFT JOIN cloud_outbox o ON o.tenant_id = r.tenant_id "
                    "AND o.entry_id = r.entry_id "
                    "WHERE r.tenant_id = ? AND r.room_id = ? "
                    "AND r.sender_agent_id = ? AND r.entry_id IN (" + placeholders + ")",
                    (tenant_id, room_id, agent_id, *entry_ids),
                ).fetchall()
        by_entry = {row["entry_id"]: row for row in rows}
        return {
            "room_id": room_id,
            "receipts": [
                {
                    "entry_id": entry_id,
                    "found": True,
                    "status": by_entry[entry_id]["read_status"],
                    "read_status": by_entry[entry_id]["read_status"],
                    "outbox_status": by_entry[entry_id]["status"] or "unknown",
                    "attempts": int(by_entry[entry_id]["attempts"] or 0),
                    "next_attempt_at": by_entry[entry_id]["next_attempt_at"],
                    "last_error": by_entry[entry_id]["last_error"],
                }
                if entry_id in by_entry else {
                    "entry_id": entry_id,
                    "found": False,
                    "status": "not_found",
                    "read_status": "not_found",
                }
                for entry_id in entry_ids
            ],
        }

    def heartbeat(self, tenant_id: str, room_id: str, agent_id: str) -> dict:
        with self.backend.transaction() as tx:
            self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, agent_id)
            now = _time.time()
            tx.execute(
                "UPDATE cloud_room_members SET last_seen = ?, status = 'active' "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (now, tenant_id, room_id, agent_id),
            )
            tx.commit()
        return {"room_id": room_id, "agent_id": agent_id, "last_seen": now, "status": "active"}

    # ------------------------------------------------------------------
    # Addressing — unicast / group / broadcast
    # ------------------------------------------------------------------

    def room_send(self, tenant_id: str, room_id: str, sender_agent_id: str,
                  target_spec: Any, payload: Any, exclude_sender: bool = True,
                  message_kind: str | None = None,
                  idempotency_key: str | None = None) -> dict:
        """Send a message to targets. Returns the event seq and per-target receipts.

        ``message_kind`` is an optional sender-set semantic label (e.g. "result"
        for a finished unit of work, "status" for liveness) stored as a
        first-class column on the event row so it is queryable without parsing
        the payload.

        ``idempotency_key`` makes the send safe to retry: a second call with
        the same key returns the ORIGINAL event's seq and receipts instead of
        creating a duplicate event or duplicate receipts. When omitted, every
        call appends a fresh event (existing behaviour).

        Atomicity: the event and ALL its delivery receipts commit in ONE
        transaction. A crash between the two leaves nothing — never a visible
        event with partial or zero receipts.
        """
        # The idempotency_key is validated at the request boundary BEFORE it is
        # used as a key in any query or insert, so a malformed value is the
        # caller's 400 invalid_argument — never a 500.
        idempotency_key = _validate_idempotency_key(idempotency_key)
        # Verify the sender is an active member BEFORE the rate gate, so a
        # non-member cannot consume the room's per-minute message budget.
        message_kind = _validate_message_kind(message_kind)
        lock_context = self._idempotency_lock if idempotency_key else nullcontext()
        with lock_context:
            # Lazy TTL close commits in its own transaction, so a refusal in
            # the delivery transaction can never roll the close back.
            self._close_expired_room(tenant_id, room_id)
            with self.backend.transaction() as tx:
                room = self._require_room(tx, tenant_id, room_id)
                self._require_member(tx, tenant_id, room_id, sender_agent_id)
                self._touch_member(tx, tenant_id, room_id, sender_agent_id)
                if room["state"] == "closed":
                    raise RoomError("room_closed", "Room is closed", 409)
                if idempotency_key:
                    existing = tx.execute(
                        "SELECT seq, payload_json, message_kind FROM cloud_room_event_log "
                        "WHERE tenant_id = ? AND room_id = ? AND origin_agent = ? "
                        "AND idempotency_key = ? AND kind = 'room.message'",
                        (tenant_id, room_id, sender_agent_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        _check_idempotency_conflict(
                            existing, payload, target_spec, exclude_sender, message_kind,
                        )
                        return {
                            "room_id": room_id,
                            "seq": int(existing["seq"]),
                            "receipts": _idempotent_receipts(
                                tx, tenant_id, room_id, sender_agent_id, existing,
                            ),
                        }
                # Validate routing BEFORE the rate gate. A send that names a
                # non-member is the caller's error (recipient_not_found) and
                # must not consume the room's per-minute message budget.
                targets = self._route_targets(tx, tenant_id, room_id, target_spec)
                self._reject_unroutable_specs(tx, tenant_id, room_id, target_spec, targets)

            # Per-minute message budget, plan-driven (PLANS is the single source of
            # truth for the limit). Refuses with rate_limited + Retry-After.
            RateLimiter().enforce(
                self.backend, tenant_id, room_id, "messages",
                plan_limits(self.backend, tenant_id).max_messages_per_minute, 60,
            )

            # Monthly event budget, plan-driven. Resolved once so the enforcement
            # inside the append transaction does not open a second connection.
            plan_id, plan = resolve_plan(self.backend, tenant_id)

            with self.backend.transaction() as tx:
                room = self._require_room(tx, tenant_id, room_id)
                self._require_member(tx, tenant_id, room_id, sender_agent_id)
                if room["state"] == "closed":
                    raise RoomError("room_closed", "Room is closed", 409)
                targets = self._route_targets(tx, tenant_id, room_id, target_spec)
                # Reject again here (membership may have changed between the
                # pre-rate validation and this delivery transaction) so a named
                # non-member can never slip through and produce a silent
                # empty-receipt success.
                self._reject_unroutable_specs(tx, tenant_id, room_id, target_spec, targets)
                if exclude_sender and sender_agent_id in targets:
                    targets = [t for t in targets if t != sender_agent_id]

                # Idempotent replay: an event with this key already exists for
                # this sender — return its original result, or reject changed
                # parameters, without writing or charging the rate window.
                if idempotency_key:
                    existing = tx.execute(
                        "SELECT seq, payload_json, message_kind FROM cloud_room_event_log "
                        "WHERE tenant_id = ? AND room_id = ? AND origin_agent = ? "
                        "AND idempotency_key = ? AND kind = 'room.message'",
                        (tenant_id, room_id, sender_agent_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        _check_idempotency_conflict(
                            existing, payload, target_spec, exclude_sender, message_kind,
                        )
                        seq = int(existing["seq"])
                        receipts = _idempotent_receipts(
                            tx, tenant_id, room_id, sender_agent_id, existing,
                        )
                        return {"room_id": room_id, "seq": seq, "receipts": receipts}

                # Enforce + count the monthly event budget in the SAME transaction
                # as the append, so a refused message leaves no counter trace.
                enforce_events_per_month(tx, tenant_id, plan_id, plan)
                seq = self._append_event(tx, tenant_id, room_id, sender_agent_id, "room.message",
                                         {"payload": payload, "target_spec": target_spec,
                                          "exclude_sender": exclude_sender,
                                          "targets": targets},
                                         message_kind=message_kind,
                                         idempotency_key=idempotency_key)
                # Build receipts (one per target) — durable via the cloud outbox,
                # committed IN THE SAME transaction as the event (all-or-nothing).
                # Each receipt is ALSO persisted as a cloud_room_receipts row so
                # its lifecycle (queued -> read on the recipient's ack past this
                # seq) survives restarts — the send response's status is no
                # longer a hardcoded literal.
                envelope_id = _new_id("oev")
                receipts = []
                now = utc_now_iso()
                for target in targets:
                    entry_id = self.backend.enqueue_outbox_in_tx(
                        tx, tenant_id, envelope_id, target,
                        _json({"room_id": room_id, "seq": seq, "payload": payload,
                               "sender": sender_agent_id}),
                    )
                    tx.execute(
                        "INSERT OR IGNORE INTO cloud_room_receipts("
                        "tenant_id, room_id, seq, recipient_agent_id, sender_agent_id, "
                        "entry_id, read_status, created_at, updated_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                        (tenant_id, room_id, seq, target, sender_agent_id,
                         entry_id, now, now),
                    )
                    receipts.append({
                        "agent_id": target,
                        "entry_id": entry_id,
                        "status": "queued",
                        "read_status": "queued",
                    })
                tx.commit()

            return {"room_id": room_id, "seq": seq, "receipts": receipts}

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def groups(self, tenant_id: str, room_id: str, agent_id: str, group_name: str,
               action: str, members: list[str] | None = None) -> dict:
        with self.backend.transaction() as tx:
            self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, agent_id)
            if action == "add":
                for member in members or []:
                    member_row = tx.execute(
                        "SELECT 1 FROM cloud_room_members WHERE tenant_id = ? AND room_id = ? AND agent_id = ? AND status = 'active'",
                        (tenant_id, room_id, member),
                    ).fetchone()
                    if member_row is None:
                        raise RoomError("member_required",
                                        f"Agent '{member}' is not an active member of this room", 400)
                    tx.execute(
                        "INSERT OR IGNORE INTO cloud_room_groups(tenant_id, room_id, group_name) VALUES (?, ?, ?)",
                        (tenant_id, room_id, group_name),
                    )
                    tx.execute(
                        "INSERT OR IGNORE INTO cloud_room_group_members(tenant_id, room_id, group_name, agent_id) VALUES (?, ?, ?, ?)",
                        (tenant_id, room_id, group_name, member),
                    )
            elif action == "remove":
                for member in members or []:
                    tx.execute(
                        "DELETE FROM cloud_room_group_members WHERE tenant_id = ? AND room_id = ? AND group_name = ? AND agent_id = ?",
                        (tenant_id, room_id, group_name, member),
                    )
            elif action != "list":
                raise RoomError("invalid_argument", "action must be add, remove, or list", 400)
            rows = tx.execute(
                "SELECT agent_id FROM cloud_room_group_members WHERE tenant_id = ? AND room_id = ? AND group_name = ? ORDER BY agent_id",
                (tenant_id, room_id, group_name),
            ).fetchall()
            member_list = [r["agent_id"] for r in rows]
            tx.commit()
        return {"room_id": room_id, "group_name": group_name, "members": member_list}

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def event_log(self, tenant_id: str, room_id: str, agent_id: str) -> list[dict]:
        """Return the full ordered event log (member-only), payload-redacted.

        Every event's payload is routed through ``_filter_payload_for_agent``
        exactly as ``poll`` does, so a legitimate member who is NOT the
        addressee of a unicast sees the redacted envelope, never the private
        body. ``SELECT *`` raw rows are never returned.
        """
        with self.backend.transaction() as tx:
            self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, agent_id)
            self._touch_member(tx, tenant_id, room_id, agent_id)
            rows = tx.execute(
                "SELECT * FROM cloud_room_event_log WHERE tenant_id = ? AND room_id = ? ORDER BY seq",
                (tenant_id, room_id),
            ).fetchall()
        events = []
        for r in rows:
            raw_payload = _parse_json(r["payload_json"], {})
            visible_payload = (
                self._filter_payload_for_agent(raw_payload, agent_id, r["origin_agent"])
                if r["kind"] == "room.message"
                else raw_payload
            )
            events.append({
                "event_id": r["event_id"],
                "seq": r["seq"],
                "origin_agent": r["origin_agent"],
                "kind": r["kind"],
                "message_kind": r["message_kind"],
                "payload": visible_payload,
                "created_at": r["created_at"],
            })
        return events
