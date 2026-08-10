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
import time as _time
import uuid
from typing import Any

from weft_cloud.storage import StorageBackend, utc_now_iso

from .identity.context import SessionContext, require_db_role
from .identity.tokens import AuthError, hash_token
from .quotas import (
    QuotaError,
    create_room_with_quota,
    enforce_events_per_month,
    increment_room_member_counter,
    join_room_with_quota,
    plan_limits,
    resolve_plan,
    validate_room_cap,
)
from .rate_limit import RateLimiter

# The public origin used to build self-describing shareable links. Configurable
# via WEFT_PUBLIC_ORIGIN; defaults to the local dev origin so the product
# works offline. No deployment URL is hardcoded.
DEFAULT_PUBLIC_ORIGIN = "http://127.0.0.1:18788"


def public_origin(origin: str | None = None) -> str:
    """Resolve the configured public origin for shareable-link URLs.

    Explicit argument wins, then the ``WEFT_PUBLIC_ORIGIN`` env var, then
    the local dev default. A trailing slash is stripped so callers can build
    ``{origin}/j/{token}`` directly.
    """
    if origin is not None:
        resolved = origin
    else:
        resolved = os.environ.get("WEFT_PUBLIC_ORIGIN", DEFAULT_PUBLIC_ORIGIN)
    resolved = str(resolved).strip().rstrip("/")
    return resolved or DEFAULT_PUBLIC_ORIGIN


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _token_hash(token: str) -> str:
    if not isinstance(token, str) or len(token) < 16 or len(token) > 512:
        raise ValueError("token must be a non-empty opaque capability")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_MESSAGE_KIND_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


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
    validated: list[str] = []
    for entry in value:
        v = _validate_message_kind(entry, "message_kinds")
        if v is not None:
            validated.append(v)
    return validated


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Persisted JSON is invalid")


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
            tx.commit()

    def _ensure_message_kind_column(self, tx: Any) -> None:
        """Add the ``message_kind`` column to an existing event log if missing."""
        row = tx.execute(
            "SELECT 1 FROM pragma_table_info('cloud_room_event_log') WHERE name = 'message_kind'"
        ).fetchone()
        if row is None:
            tx.execute("ALTER TABLE cloud_room_event_log ADD COLUMN message_kind TEXT")

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

    def _resolve_room_tenant(self, tx: Any, room_id: str, agent_id: str | None = None,
                             actor_token_hash: str | None = None) -> str:
        """Find the tenant_id for a room, optionally scoped to a member.

        Used when the caller's session tenant may differ from the room's
        owning tenant (cross-tenant join via link). If agent_id is given,
        only returns the tenant if the agent is an active member.

        ``actor_token_hash`` binds the resolution to a specific credential:
        when supplied, the membership row must ALSO carry that actor token
        hash. This is the cross-tenant impersonation fix — an attacker who
        holds another tenant's member ``agent_id`` but not the actor token
        that joined resolves the same uniform ``room_not_found`` as a room
        that never existed (no existence oracle).
        """
        if agent_id:
            query = (
                "SELECT tenant_id FROM cloud_room_members "
                "WHERE room_id = ? AND agent_id = ? AND status = 'active'"
            )
            params: list[Any] = [room_id, agent_id]
            if actor_token_hash is not None:
                query += " AND actor_token_hash = ?"
                params.append(actor_token_hash)
            row = tx.execute(query, params).fetchone()
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

    @staticmethod
    def _filter_payload_for_agent(payload: dict, agent_id: str) -> dict:
        """Redact message payloads not addressed to ``agent_id``.

        For ``room.message`` events, only agents listed in ``targets`` (or
        everyone for broadcast ``target_spec == "*"``) may see the payload.
        Non-addressees receive a redacted envelope so the ordered event
        sequence stays visible without leaking the body.
        """
        if not isinstance(payload, dict):
            return payload
        if payload.get("target_spec") == "*":
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
        """Expand a target spec over the room's own active member set."""
        member_rows = tx.execute(
            "SELECT agent_id, last_seen FROM cloud_room_members "
            "WHERE tenant_id = ? AND room_id = ? AND status = 'active'",
            (tenant_id, room_id),
        ).fetchall()
        active_ids: set[str] = set()
        for row in member_rows:
            age = max(0.0, _time.time() - float(row["last_seen"]))
            if age <= 1800:
                active_ids.add(row["agent_id"])

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
            if spec in groups:
                return groups[spec] & active_ids
            if spec in active_ids:
                return {spec}
            return set()

        if isinstance(target_spec, str):
            specs = [target_spec]
        else:
            specs = list(target_spec or [])

        result: set[str] = set()
        for spec in specs:
            result |= _expand(spec)
        return sorted(result)

    # ------------------------------------------------------------------
    # Room lifecycle
    # ------------------------------------------------------------------

    def create_room(self, tenant_id: str, owner_agent_id: str, actor_token: str,
                    cap: int = 10, name: str | None = None, ttl_seconds: int = 86400,
                    origin: str | None = None) -> dict:
        """Create a room and return its shareable link.

        The owner auto-joins as the first active member. The raw link token is
        returned EXACTLY ONCE — only its SHA-256 is stored.

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

        # Quota gates — both atomic in their own transactions (BEGIN IMMEDIATE).
        # 1. Enforce the tenant's max_rooms cap; records the room binding and
        #    increments the rooms counter in the SAME transaction as the check.
        # 2. Count the owner's auto-join in the room's member counter so the
        #    plan member cap counts the owner exactly like every other join.
        create_room_with_quota(self.backend, tenant_id, room_id, self.backend.state_path)
        join_room_with_quota(self.backend, tenant_id, room_id, owner_agent_id)

        with self.backend.transaction() as tx:
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
        link_hash = _token_hash(link_token)
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
        multi-use up to the cap — it is NOT consumed. Re-join with the same
        agent_id is idempotent.

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
                # Existing active identity: verify it is the SAME actor, then
                # refresh presence. Idempotent re-joins are NOT re-counted
                # against the plan member cap and never re-gated by the room cap.
                if existing["actor_token_hash"] and existing["actor_token_hash"] != actor_token_hash:
                    raise RoomError("actor_auth_invalid",
                                    "Link cannot overwrite an existing member identity", 403)
                now = utc_now_iso()
                tx.execute(
                    "UPDATE cloud_room_members SET status = 'active', last_seen = ?, "
                    "joined_at = ?, capabilities_json = ?, actor_token_hash = ? "
                    "WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                    (now_epoch, now, _json(caps), actor_token_hash,
                     real_tenant_id, room_id, agent_id),
                )
                tx.execute(
                    "INSERT INTO cloud_room_cursors(tenant_id, room_id, agent_id, last_ack_seq, updated_at) "
                    "VALUES (?, ?, ?, 0, ?) "
                    "ON CONFLICT(tenant_id, room_id, agent_id) DO NOTHING",
                    (real_tenant_id, room_id, agent_id, now),
                )
                tx.commit()
                return {"room_id": room_id, "agent_id": agent_id, "status": "active",
                        "joined_at": now, "cursor": 0}

            # New identity OR a left member reactivating — both need a seat.
            # The plan member cap check + counter increment run here (they roll
            # back with this transaction if the room's own cap below refuses),
            # and the room cap is enforced before the membership is recorded.
            # BEGIN IMMEDIATE serializes concurrent joins, so exactly one can
            # take the last slot.
            if existing is not None and existing["actor_token_hash"] and existing["actor_token_hash"] != actor_token_hash:
                raise RoomError("actor_auth_invalid",
                                "Link cannot overwrite an existing member identity", 403)
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
            tx.commit()

        return {"room_id": room_id, "agent_id": agent_id, "status": "active",
                "joined_at": now, "cursor": 0}

    def room_info(self, tenant_id: str, room_id: str, agent_id: str) -> dict:
        """Member-only room summary with roster and presence."""
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, agent_id)
            members = tx.execute(
                "SELECT * FROM cloud_room_members WHERE tenant_id = ? AND room_id = ? AND status = 'active' ORDER BY joined_at",
                (tenant_id, room_id),
            ).fetchall()
            member_list = []
            for m in members:
                age = max(0.0, _time.time() - float(m["last_seen"]))
                member_list.append({
                    "agent_id": m["agent_id"],
                    "status": "stale" if age > 1800 else "active",
                    "capabilities": _parse_json(m["capabilities_json"], []),
                    "last_seen": float(m["last_seen"]),
                    "joined_at": m["joined_at"],
                })
            return {
                "room_id": room_id,
                "name": room["name"],
                "state": room["state"],
                "cap": room["cap"],
                "member_count": len(member_list),
                "members": member_list,
                "owner_agent_id": room["owner_agent_id"],
            }

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
        """Revoke a specific link (owner only)."""
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, owner_agent_id)
            if room["owner_agent_id"] != owner_agent_id:
                raise RoomError("owner_required", "Only the room owner can revoke links", 403)
            tx.execute(
                "UPDATE cloud_room_links SET revoked = 1 WHERE link_id = ? AND tenant_id = ? AND room_id = ?",
                (link_id, tenant_id, room_id),
            )
            tx.commit()
        return {"link_id": link_id, "revoked": True}

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
        """Return events with ``seq > after_seq`` (default: last_ack_seq).

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
            if after_seq is None:
                cursor = tx.execute(
                    "SELECT last_ack_seq FROM cloud_room_cursors WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                    (tenant_id, room_id, agent_id),
                ).fetchone()
                after_seq = int(cursor["last_ack_seq"]) if cursor else 0
            limit = max(1, min(int(limit), 200))
            kind_filter = _validate_message_kinds(message_kinds)
            if kind_filter:
                placeholders = ", ".join("?" for _ in kind_filter)
                rows = tx.execute(
                    "SELECT * FROM cloud_room_event_log WHERE tenant_id = ? AND room_id = ? "
                    "AND seq > ? AND message_kind IN (" + placeholders + ") "
                    "ORDER BY seq ASC LIMIT ?",
                    (tenant_id, room_id, after_seq, *kind_filter, limit),
                ).fetchall()
            else:
                rows = tx.execute(
                    "SELECT * FROM cloud_room_event_log WHERE tenant_id = ? AND room_id = ? AND seq > ? "
                    "ORDER BY seq ASC LIMIT ?",
                    (tenant_id, room_id, after_seq, limit),
                ).fetchall()
            events = []
            next_seq = after_seq
            for r in rows:
                raw_payload = _parse_json(r["payload_json"], {})
                events.append({
                    "event_id": r["event_id"],
                    "seq": r["seq"],
                    "origin_agent": r["origin_agent"],
                    "kind": r["kind"],
                    "message_kind": r["message_kind"],
                    "payload": self._filter_payload_for_agent(raw_payload, agent_id),
                    "created_at": r["created_at"],
                })
                next_seq = r["seq"] + 1
            cursor_row = tx.execute(
                "SELECT last_ack_seq FROM cloud_room_cursors WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, agent_id),
            ).fetchone()
            last_ack = int(cursor_row["last_ack_seq"]) if cursor_row else 0
            return {
                "room_id": room_id,
                "state": room["state"],
                "events": events,
                "next_seq": next_seq,
                "cursor_head": int(room["cursor_head"]),
                "last_ack_seq": last_ack,
                "has_more": len(rows) == limit,
            }

    def wait(self, tenant_id: str, room_id: str, agent_id: str,
             after_seq: int | None = None, timeout_seconds: int = 20,
             limit: int = 100, message_kinds: list[str] | None = None) -> dict:
        """Blocking long-poll over ``poll``: the continuous-collaboration primitive.

        Returns as soon as at least one event with ``seq > after_seq`` is
        available; otherwise returns an EMPTY poll result at the timeout — a
        NORMAL outcome, not an error, so a caller simply loops again. This is
        what keeps an agent inside its turn: ``wait, react, wait again`` with
        no human in the loop.

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
        """
        try:
            timeout_seconds = int(timeout_seconds)
        except (TypeError, ValueError):
            timeout_seconds = 20
        timeout_seconds = max(0, min(timeout_seconds, 30))
        deadline = _time.monotonic() + timeout_seconds
        result = self.poll(tenant_id, room_id, agent_id, after_seq, limit,
                           message_kinds=message_kinds)
        if not result["events"]:
            pinned_after = int(result["next_seq"])
            while _time.monotonic() < deadline:
                _time.sleep(0.25)
                result = self.poll(tenant_id, room_id, agent_id, pinned_after, limit,
                                   message_kinds=message_kinds)
                if result["events"]:
                    break
        result["timed_out"] = not bool(result["events"])
        return result

    def ack(self, tenant_id: str, room_id: str, agent_id: str, seq: int) -> dict:
        """Acknowledge events up to ``seq`` (monotonic)."""
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, agent_id)
            if int(seq) > int(room["cursor_head"]):
                raise RoomError("invalid_cursor", "Cannot acknowledge an event beyond the room head", 400)
            now = utc_now_iso()
            tx.execute(
                "INSERT INTO cloud_room_cursors(tenant_id, room_id, agent_id, last_ack_seq, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, room_id, agent_id) DO UPDATE SET "
                "last_ack_seq = MAX(last_ack_seq, excluded.last_ack_seq), updated_at = excluded.updated_at",
                (tenant_id, room_id, agent_id, int(seq), now),
            )
            row = tx.execute(
                "SELECT last_ack_seq FROM cloud_room_cursors WHERE tenant_id = ? AND room_id = ? AND agent_id = ?",
                (tenant_id, room_id, agent_id),
            ).fetchone()
            tx.commit()
        return {"room_id": room_id, "agent_id": agent_id, "last_ack_seq": int(row["last_ack_seq"])}

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
        # Verify the sender is an active member BEFORE the rate gate, so a
        # non-member cannot consume the room's per-minute message budget.
        message_kind = _validate_message_kind(message_kind)
        with self.backend.transaction() as tx:
            room = self._require_room(tx, tenant_id, room_id)
            self._require_member(tx, tenant_id, room_id, sender_agent_id)
            if room["state"] == "closed":
                raise RoomError("room_closed", "Room is closed", 409)

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
            if exclude_sender and sender_agent_id in targets:
                targets = [t for t in targets if t != sender_agent_id]

            # Idempotent replay: an event with this key already exists for this
            # sender — return its seq and its original receipts, write nothing.
            if idempotency_key:
                existing = tx.execute(
                    "SELECT seq FROM cloud_room_event_log "
                    "WHERE tenant_id = ? AND room_id = ? AND origin_agent = ? "
                    "AND idempotency_key = ? AND kind = 'room.message'",
                    (tenant_id, room_id, sender_agent_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    seq = int(existing["seq"])
                    payload_json = _json({"room_id": room_id, "seq": seq,
                                          "payload": payload, "sender": sender_agent_id})
                    rows = tx.execute(
                        "SELECT entry_id, recipient FROM cloud_outbox "
                        "WHERE tenant_id = ? AND payload_json = ? ORDER BY recipient",
                        (tenant_id, payload_json),
                    ).fetchall()
                    receipts = [{"agent_id": r["recipient"], "entry_id": r["entry_id"],
                                 "status": "queued"} for r in rows]
                    return {"room_id": room_id, "seq": seq, "receipts": receipts}

            # Enforce + count the monthly event budget in the SAME transaction
            # as the append, so a refused message leaves no counter trace.
            enforce_events_per_month(tx, tenant_id, plan_id, plan)
            seq = self._append_event(tx, tenant_id, room_id, sender_agent_id, "room.message",
                                     {"payload": payload, "target_spec": target_spec,
                                      "targets": targets},
                                     message_kind=message_kind,
                                     idempotency_key=idempotency_key)
            # Build receipts (one per target) — durable via the cloud outbox,
            # committed IN THE SAME transaction as the event (all-or-nothing).
            envelope_id = _new_id("oev")
            receipts = []
            for target in targets:
                entry_id = self.backend.enqueue_outbox_in_tx(
                    tx, tenant_id, envelope_id, target,
                    _json({"room_id": room_id, "seq": seq, "payload": payload,
                           "sender": sender_agent_id}),
                )
                receipts.append({"agent_id": target, "entry_id": entry_id, "status": "queued"})
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
            rows = tx.execute(
                "SELECT * FROM cloud_room_event_log WHERE tenant_id = ? AND room_id = ? ORDER BY seq",
                (tenant_id, room_id),
            ).fetchall()
        events = []
        for r in rows:
            events.append({
                "event_id": r["event_id"],
                "seq": r["seq"],
                "origin_agent": r["origin_agent"],
                "kind": r["kind"],
                "message_kind": r["message_kind"],
                "payload": self._filter_payload_for_agent(
                    _parse_json(r["payload_json"], {}), agent_id),
                "created_at": r["created_at"],
            })
        return events
