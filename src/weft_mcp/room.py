"""Weft Room product object — Wave E.

A Room is one multi-use link admitting N agents (bounded by a cap), each with
its own identity and capabilities. Members see roster/presence and an ordered
event log replayed from their own per-member cursor. Any member addresses one
agent, a named group, or the whole room with durable delivery receipts.

This is COMPOSITION over existing primitives — roster.py (N-way roster,
groups, route_targets, build_envelope_v2), outbox.py (durable per-recipient
delivery), core.py (consent, actor credentials, leases, fencing, evidence).
New state lives in room_-prefixed tables in the same SQLite file. Stdlib only,
no shell execution from payloads.

Authoritative spec: docs/ROOMS_DESIGN.md.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Sequence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _epoch() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _validate_id(value: str, field: str) -> str:
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError(f"{field} must be 1-128 safe identifier characters")
    return value


def _validate_message_kind(value: Any, field: str = "message_kind") -> str | None:
    """Validate a sender-set ``message_kind`` (optional, lowercase slug).

    ``None`` is allowed (message_kind is optional). Otherwise it must be a
    lowercase ``[a-z0-9_-]`` string of at most 32 characters. The error names
    the argument so callers get a clear message.
    """
    import re
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", value):
        raise RoomError(
            "invalid_argument",
            f"{field} must be an optional string of 1-32 lowercase characters matching [a-z0-9_-]",
        )
    return value


def _validate_message_kinds(value: Sequence[str] | None,
                            field: str = "message_kinds") -> list[str] | None:
    """Validate the optional ``message_kinds`` poll filter.

    ``None`` means "no filter" (return everything). Otherwise it must be a
    sequence of message-kind strings, each validated by ``_validate_message_kind``.
    """
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise RoomError(
            "invalid_argument",
            f"{field} must be an optional list of message_kind strings",
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
    except json.JSONDecodeError as exc:
        raise ValueError("Persisted JSON is invalid") from exc


def _token_hash(token: str) -> str:
    if not isinstance(token, str) or len(token) < 16 or len(token) > 512:
        raise ValueError("Token must be a non-empty opaque capability")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Public origin used to build self-describing shareable links for rooms.
# Configurable via WEFT_PUBLIC_ORIGIN; defaults to the local dev origin so
# the coordinator works offline. No deployment URL is hardcoded. (Same default
# as the cloud service; the coordinator plane cannot import the cloud plane.)
DEFAULT_PUBLIC_ORIGIN = "http://127.0.0.1:18788"


def _public_origin(origin: str | None = None) -> str:
    if origin is not None:
        resolved = origin
    else:
        resolved = os.environ.get("WEFT_PUBLIC_ORIGIN", DEFAULT_PUBLIC_ORIGIN)
    resolved = str(resolved).strip().rstrip("/")
    return resolved or DEFAULT_PUBLIC_ORIGIN


# ---------------------------------------------------------------------------
# Schema — room_-prefixed, additive, no existing table modified
# ---------------------------------------------------------------------------

_ROOM_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS room_rooms (
    room_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS room_members (
    room_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    last_seen REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','stale','left')),
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    actor_token_hash TEXT NOT NULL,
    PRIMARY KEY (room_id, agent_id)
);

CREATE TABLE IF NOT EXISTS room_links (
    link_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_room_links_room ON room_links(room_id);

CREATE TABLE IF NOT EXISTS room_event_log (
    event_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_room_events_replay ON room_event_log(room_id, seq);

CREATE TABLE IF NOT EXISTS room_cursors (
    room_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    last_ack_seq INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (room_id, agent_id)
);

CREATE TABLE IF NOT EXISTS room_groups (
    room_id TEXT NOT NULL,
    group_name TEXT NOT NULL,
    PRIMARY KEY (room_id, group_name)
);

CREATE TABLE IF NOT EXISTS room_group_members (
    room_id TEXT NOT NULL,
    group_name TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    PRIMARY KEY (room_id, group_name, agent_id)
);
"""


class RoomError(Exception):
    """Carries a Weft-compatible error code for the MCP surface."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RoomStore:
    """SQLite-backed room state, coexisting with core/roster/outbox tables."""

    def __init__(self, db_path: str | os.PathLike[str], origin: str | None = None):
        self.db_path = str(Path(db_path).expanduser().resolve())
        self.origin = _public_origin(origin)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=15,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextlib.contextmanager
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

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_ROOM_SCHEMA_SQL)
            self._ensure_message_kind_column(connection)
        finally:
            connection.close()

    def _ensure_message_kind_column(self, conn: sqlite3.Connection) -> None:
        """Add the ``message_kind`` column to an existing event log if missing."""
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info('room_event_log') WHERE name = 'message_kind'"
        ).fetchone()
        if row is None:
            conn.execute("ALTER TABLE room_event_log ADD COLUMN message_kind TEXT")

    # ------------------------------------------------------------------
    # Room lifecycle
    # ------------------------------------------------------------------

    def create_room(self, team_id: str, owner_agent_id: str, cap: int, actor_token_hash: str,
                    name: str | None = None, ttl_seconds: int = 86400) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(owner_agent_id, "owner_agent_id")
        if not isinstance(cap, int) or cap < 2:
            raise RoomError("invalid_argument", "cap must be an integer >= 2")
        if not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise RoomError("invalid_argument", "ttl_seconds must be a positive integer")
        room_id = _new_id("room")
        link_id = _new_id("link")
        raw_token = f"rm_{secrets.token_urlsafe(32)}"
        now = _utc_now()
        expires_at = _epoch() + ttl_seconds
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO room_rooms(room_id, team_id, owner_agent_id, name, cap, state, link_id, created_at, expires_at, cursor_head) "
                "VALUES (?, ?, ?, ?, ?, 'forming', ?, ?, ?, 0)",
                (room_id, team_id, owner_agent_id, name, cap, link_id, now, expires_at),
            )
            conn.execute(
                "INSERT INTO room_links(link_id, room_id, token_hash, created_by, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (link_id, room_id, _token_hash(raw_token), owner_agent_id, now, expires_at),
            )
            # Owner auto-joins as the first active member.
            conn.execute(
                "INSERT INTO room_members(room_id, agent_id, joined_at, last_seen, status, capabilities_json, actor_token_hash) "
                "VALUES (?, ?, ?, ?, 'active', '[]', ?)",
                (room_id, owner_agent_id, now, _epoch(), actor_token_hash),
            )
            conn.execute(
                "INSERT INTO room_cursors(room_id, agent_id, last_ack_seq, updated_at) VALUES (?, ?, 0, ?)",
                (room_id, owner_agent_id, now),
            )
            self._append_event(conn, room_id, owner_agent_id, "room.created",
                               {"room_id": room_id, "owner_agent_id": owner_agent_id, "cap": cap})
            self._append_event(conn, room_id, owner_agent_id, "room.joined",
                               {"agent_id": owner_agent_id, "status": "active"})
        return {
            "room_id": room_id,
            "link_id": link_id,
            "link_token": raw_token,
            "shareable_link": f"{self.origin}/j/{raw_token}",
            "expires_at": expires_at,
            "cap": cap,
            "state": "forming",
            "owner_agent_id": owner_agent_id,
        }

    def _append_event(self, conn: sqlite3.Connection, room_id: str, origin: str, kind: str,
                      payload: Any, idempotency_key: str | None = None,
                      message_kind: str | None = None) -> int:
        row = conn.execute(
            "SELECT cursor_head FROM room_rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        seq = int(row["cursor_head"]) + 1
        event_id = _new_id("revt")
        if idempotency_key is None:
            idempotency_key = f"{room_id}:{kind}:{_new_id('idem')}"
        conn.execute(
            "INSERT INTO room_event_log(event_id, room_id, seq, origin_agent, kind, message_kind, payload_json, idempotency_key, trace_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (event_id, room_id, seq, origin, kind, message_kind, _json(payload), idempotency_key, _utc_now()),
        )
        conn.execute(
            "UPDATE room_rooms SET cursor_head = ? WHERE room_id = ?",
            (seq, room_id),
        )
        return seq

    def _require_room(self, conn: sqlite3.Connection, room_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM room_rooms WHERE room_id = ?", (room_id,)).fetchone()
        if row is None:
            raise RoomError("room_not_found", "Room not found")
        return row

    def _require_member(self, conn: sqlite3.Connection, room_id: str, agent_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM room_members WHERE room_id = ? AND agent_id = ? AND status = 'active'",
            (room_id, agent_id),
        ).fetchone()
        if row is None:
            # No-oracle path: a caller who is not an active member must not be
            # able to learn whether the room exists. `room_not_found` here is
            # indistinguishable from `_require_room` failing on a room that
            # never existed. This matches the hosted cloud plane, which resolves
            # the room through the caller's membership and returns 404 for a
            # non-member. (Members are still entitled to precise errors.)
            raise RoomError("room_not_found", "Room not found")
        return row

    def _validate_actor(self, conn: sqlite3.Connection, team_id: str, agent_id: str,
                        actor_token: str, actor_token_hash: str | None) -> None:
        """Validate the supplied actor token against the stored credential hash."""
        if not isinstance(actor_token, str) or len(actor_token) < 16:
            raise RoomError("actor_auth_invalid", "Actor token is invalid")
        supplied_hash = _token_hash(actor_token)
        if actor_token_hash is not None and supplied_hash != actor_token_hash:
            raise RoomError("actor_auth_invalid", "Actor token is invalid")
        # Cross-check against the registered agent credential (same as core).
        row = conn.execute(
            "SELECT token_hash FROM agent_credentials WHERE team_id = ? AND agent_id = ? AND revoked_at IS NULL",
            (team_id, agent_id),
        ).fetchone()
        if row is None or not secrets.compare_digest(row["token_hash"], supplied_hash):
            raise RoomError("actor_auth_invalid", "Actor token is invalid")

    def _require_authenticated_member(self, conn: sqlite3.Connection, team_id: str,
                                      room_id: str, agent_id: str, actor_token: str) -> sqlite3.Row:
        """Gate a member-only room operation: authenticate, then resolve, then check membership.

        Ordering IS the security contract. Credential validation precedes room
        resolution, so a caller with a bad or missing token gets
        ``actor_auth_invalid`` regardless of whether the room exists — the same
        response for a real room and a fabricated one (no existence oracle).
        A valid-token non-member then gets the flattened ``room_not_found``,
        while a genuine member keeps precise errors.
        """
        self._validate_actor(conn, team_id, agent_id, actor_token, None)
        room = self._require_room(conn, room_id)
        self._require_member(conn, room_id, agent_id)
        return room

    def join_room(self, team_id: str, room_id: str, link_token: str, agent_id: str,
                  consent: Any, capabilities: Sequence[str], actor_token: str,
                  actor_token_hash: str) -> dict[str, Any]:
        _validate_id(agent_id, "agent_id")
        if consent is not True:
            raise RoomError("consent_required", "consent must be the literal JSON boolean true")
        with self._transaction() as conn:
            room = self._require_room(conn, room_id)
            if room["state"] == "closed":
                raise RoomError("room_closed", "Room is closed")
            link = conn.execute(
                "SELECT * FROM room_links WHERE room_id = ? AND token_hash = ?",
                (room_id, _token_hash(link_token)),
            ).fetchone()
            if link is None:
                raise RoomError("invalid_link", "Link is not valid for this room")
            if link["revoked"]:
                raise RoomError("link_revoked", "Link has been revoked")
            if float(link["expires_at"]) < _epoch():
                raise RoomError("link_expired", "Link has expired")

            self._validate_actor(conn, team_id, agent_id, actor_token, actor_token_hash)

            existing = conn.execute(
                "SELECT * FROM room_members WHERE room_id = ? AND agent_id = ?",
                (room_id, agent_id),
            ).fetchone()
            if existing is not None:
                # Existing identity: verify it is the SAME actor, then reactivate.
                # A blank stored hash means the owner auto-joined in trusted mode
                # without a token; the first explicit join BINDS that identity.
                if existing["actor_token_hash"] and existing["actor_token_hash"] != actor_token_hash:
                    raise RoomError("actor_auth_invalid", "Link cannot overwrite an existing member identity")
                now = _utc_now()
                already_active = existing["status"] == "active"
                if already_active:
                    # Idempotent re-join of an already-active member: refresh
                    # presence ONLY. joined_at is a one-time fact — rewriting it
                    # churned the roster on every client retry. Capabilities ARE
                    # recorded when the caller states them (explicit intent; an
                    # idempotent retry re-sends the same list, and an omitted
                    # list is indistinguishable from no intent, so keep the
                    # stored value). The actor-token hash stays as the original
                    # join recorded.
                    if capabilities:
                        conn.execute(
                            "UPDATE room_members SET last_seen = ?, capabilities_json = ? "
                            "WHERE room_id = ? AND agent_id = ?",
                            (_epoch(), _json(list(capabilities)), room_id, agent_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE room_members SET last_seen = ? WHERE room_id = ? AND agent_id = ?",
                            (_epoch(), room_id, agent_id),
                        )
                    joined_at = existing["joined_at"]
                else:
                    # Left member reactivating: this IS a new membership period.
                    conn.execute(
                        "UPDATE room_members SET status = 'active', last_seen = ?, joined_at = ?, capabilities_json = ?, actor_token_hash = ? "
                        "WHERE room_id = ? AND agent_id = ?",
                        (_epoch(), now, _json(list(capabilities)), actor_token_hash, room_id, agent_id),
                    )
                    joined_at = now
                conn.execute(
                    "INSERT INTO room_cursors(room_id, agent_id, last_ack_seq, updated_at) VALUES (?, ?, 0, ?) "
                    "ON CONFLICT(room_id, agent_id) DO NOTHING",
                    (room_id, agent_id, now),
                )
                # Idempotent re-join of an already-active member emits NO event.
                if not already_active:
                    self._append_event(conn, room_id, agent_id, "room.joined",
                                       {"agent_id": agent_id, "status": "active"})
                cursor_row = conn.execute(
                    "SELECT last_ack_seq FROM room_cursors WHERE room_id = ? AND agent_id = ?",
                    (room_id, agent_id),
                ).fetchone()
                cursor = int(cursor_row["last_ack_seq"]) if cursor_row else 0
                return {"room_id": room_id, "agent_id": agent_id, "status": "active",
                        "joined_at": joined_at, "cursor": cursor}

            # New identity: enforce the cap atomically (BEGIN IMMEDIATE writer lock).
            active_count = conn.execute(
                "SELECT COUNT(*) AS c FROM room_members WHERE room_id = ? AND status = 'active'",
                (room_id,),
            ).fetchone()["c"]
            if active_count >= int(room["cap"]):
                raise RoomError("room_full", f"Room is full (cap {room['cap']})")

            now = _utc_now()
            conn.execute(
                "INSERT INTO room_members(room_id, agent_id, joined_at, last_seen, status, capabilities_json, actor_token_hash) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?)",
                (room_id, agent_id, now, _epoch(), _json(list(capabilities)), actor_token_hash),
            )
            conn.execute(
                "INSERT INTO room_cursors(room_id, agent_id, last_ack_seq, updated_at) VALUES (?, ?, 0, ?)",
                (room_id, agent_id, now),
            )
            conn.execute(
                "UPDATE room_rooms SET state = 'active' WHERE room_id = ? AND state = 'forming'",
                (room_id,),
            )
            self._append_event(conn, room_id, agent_id, "room.joined",
                               {"agent_id": agent_id, "status": "active"})
            return {"room_id": room_id, "agent_id": agent_id, "status": "active",
                    "joined_at": now, "cursor": 0}

    def leave_room(self, team_id: str, room_id: str, agent_id: str, actor_token: str) -> dict[str, Any]:
        _validate_id(agent_id, "agent_id")
        with self._transaction() as conn:
            self._require_authenticated_member(conn, team_id, room_id, agent_id, actor_token)
            conn.execute(
                "UPDATE room_members SET status = 'left' WHERE room_id = ? AND agent_id = ?",
                (room_id, agent_id),
            )
            self._append_event(conn, room_id, agent_id, "room.left", {"agent_id": agent_id})
            return {"room_id": room_id, "agent_id": agent_id, "status": "left"}

    def close_room(self, team_id: str, room_id: str, caller_agent_id: str, actor_token: str) -> dict[str, Any]:
        _validate_id(caller_agent_id, "caller_agent_id")
        with self._transaction() as conn:
            room = self._require_authenticated_member(conn, team_id, room_id, caller_agent_id, actor_token)
            if room["owner_agent_id"] != caller_agent_id:
                raise RoomError("owner_required", "Only the room owner can close it")
            conn.execute(
                "UPDATE room_rooms SET state = 'closed' WHERE room_id = ?",
                (room_id,),
            )
            conn.execute(
                "UPDATE room_links SET revoked = 1 WHERE room_id = ?",
                (room_id,),
            )
            self._append_event(conn, room_id, caller_agent_id, "room.closed", {"room_id": room_id})
            return {"room_id": room_id, "state": "closed"}

    def revoke_link(self, team_id: str, room_id: str, owner_agent_id: str, link_id: str,
                    actor_token: str) -> dict[str, Any]:
        # Error-code decision (2026-08-12 finding: revoke reported success while
        # revoking nothing). A MALFORMED link_id is the caller's error: refuse it
        # with invalid_argument BEFORE any room/link lookup. A WELL-FORMED but
        # unknown, already-revoked, or wrong-room link_id all collapse to the
        # SAME link_not_found response — byte-identical for a link that lives in
        # someone else's room and a link that never existed, so this endpoint is
        # not a link-id existence oracle (the codebase has had three of those;
        # this must not be a fourth). Success requires that exactly one row
        # flipped: if zero rows changed, nothing was revoked and returning 200
        # would manufacture false confidence exactly when an owner is cutting
        # off a leaked link.
        try:
            _validate_id(link_id, "link_id")
        except ValueError as exc:
            raise RoomError("invalid_argument", str(exc)) from exc
        with self._transaction() as conn:
            room = self._require_authenticated_member(conn, team_id, room_id, owner_agent_id, actor_token)
            if room["owner_agent_id"] != owner_agent_id:
                raise RoomError("owner_required", "Only the room owner can revoke links")
            link = conn.execute(
                "SELECT link_id, revoked FROM room_links WHERE link_id = ? AND room_id = ?",
                (link_id, room_id),
            ).fetchone()
            if link is None or link["revoked"]:
                raise RoomError("link_not_found", "Link not found for this room")
            cursor = conn.execute(
                "UPDATE room_links SET revoked = 1 WHERE link_id = ? AND room_id = ? AND revoked = 0",
                (link_id, room_id),
            )
            if cursor.rowcount != 1:
                raise RoomError("link_not_found", "Link not found for this room")
            return {"link_id": link_id, "revoked": True}

    # ------------------------------------------------------------------
    # Reads — info, presence, poll
    # ------------------------------------------------------------------

    def room_info(self, team_id: str, room_id: str, agent_id: str, actor_token: str) -> dict[str, Any]:
        with self._transaction() as conn:
            room = self._require_authenticated_member(conn, team_id, room_id, agent_id, actor_token)
            members = conn.execute(
                "SELECT * FROM room_members WHERE room_id = ? AND status = 'active' ORDER BY joined_at",
                (room_id,),
            ).fetchall()
            member_list = []
            for m in members:
                age = max(0.0, _epoch() - float(m["last_seen"]))
                member_list.append({
                    "agent_id": m["agent_id"],
                    "status": "stale" if age > 1800 else "active",
                    "capabilities": _parse_json(m["capabilities_json"], []),
                    "last_seen": float(m["last_seen"]),
                    "joined_at": m["joined_at"],
                })
            result = {
                "room_id": room_id,
                "state": room["state"],
                "cap": room["cap"],
                "member_count": len(member_list),
                "members": member_list,
                "owner_agent_id": room["owner_agent_id"],
            }
            # Owner-only link control surface (2026-08-12 finding: link_id was
            # unobtainable after room_create). A room has exactly one link
            # (created with the room), so exposing it on room_info gives the
            # owner the identifier room_revoke_link needs WITHOUT a new endpoint
            # and WITHOUT leaking it to members who cannot revoke. link_id is a
            # control-plane identifier; link_token — the bearer credential — is
            # never exposed here. link_revoked lets the owner confirm a
            # revocation actually landed.
            if room["owner_agent_id"] == agent_id:
                link = conn.execute(
                    "SELECT link_id, revoked FROM room_links WHERE room_id = ? LIMIT 1",
                    (room_id,),
                ).fetchone()
                if link is not None:
                    result["link_id"] = link["link_id"]
                    result["link_revoked"] = bool(link["revoked"])
            return result

    def heartbeat(self, team_id: str, room_id: str, agent_id: str, actor_token: str) -> dict[str, Any]:
        with self._transaction() as conn:
            self._require_authenticated_member(conn, team_id, room_id, agent_id, actor_token)
            now = _epoch()
            conn.execute(
                "UPDATE room_members SET last_seen = ?, status = 'active' WHERE room_id = ? AND agent_id = ?",
                (now, room_id, agent_id),
            )
            return {"room_id": room_id, "agent_id": agent_id, "last_seen": now, "status": "active"}

    def poll(self, team_id: str, room_id: str, agent_id: str, actor_token: str,
             after_seq: int | None = None, limit: int = 100,
             message_kinds: Sequence[str] | None = None) -> dict[str, Any]:
        """Replay ordered Room events after a cursor.

        ``message_kinds`` is an optional list of sender-set message kinds.
        When present, only events whose ``message_kind`` matches one of the
        entries are returned; when absent, behaviour is exactly as before
        (everything is returned). This filters only the ``events`` list —
        ``next_seq`` and ``cursor_head`` are always reported against the FULL
        stream, so a filtering caller can page through matching events with no
        gaps or repeats and can ack ``cursor_head`` to keep its durable cursor
        in exact agreement with an unfiltered view.
        """
        kind_filter = _validate_message_kinds(message_kinds)
        with self._transaction() as conn:
            room = self._require_authenticated_member(conn, team_id, room_id, agent_id, actor_token)
            if after_seq is None:
                cursor = conn.execute(
                    "SELECT last_ack_seq FROM room_cursors WHERE room_id = ? AND agent_id = ?",
                    (room_id, agent_id),
                ).fetchone()
                after_seq = int(cursor["last_ack_seq"]) if cursor else 0
            limit = max(1, min(int(limit), 200))
            if kind_filter:
                placeholders = ", ".join("?" for _ in kind_filter)
                rows = conn.execute(
                    "SELECT * FROM room_event_log WHERE room_id = ? AND seq > ? "
                    "AND message_kind IN (" + placeholders + ") ORDER BY seq ASC LIMIT ?",
                    (room_id, after_seq, *kind_filter, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM room_event_log WHERE room_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                    (room_id, after_seq, limit),
                ).fetchall()
            events = []
            next_seq = after_seq
            for r in rows:
                events.append({
                    "event_id": r["event_id"],
                    "seq": r["seq"],
                    "origin_agent": r["origin_agent"],
                    "kind": r["kind"],
                    "message_kind": r["message_kind"],
                    "payload": _parse_json(r["payload_json"], {}),
                    "created_at": r["created_at"],
                })
                next_seq = r["seq"] + 1
            cursor_row = conn.execute(
                "SELECT last_ack_seq FROM room_cursors WHERE room_id = ? AND agent_id = ?",
                (room_id, agent_id),
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

    def ack(self, team_id: str, room_id: str, agent_id: str, seq: int, actor_token: str) -> dict[str, Any]:
        with self._transaction() as conn:
            room = self._require_authenticated_member(conn, team_id, room_id, agent_id, actor_token)
            if int(seq) > int(room["cursor_head"]):
                raise RoomError("invalid_cursor", "Cannot acknowledge an event beyond the room head")
            now = _utc_now()
            conn.execute(
                "INSERT INTO room_cursors(room_id, agent_id, last_ack_seq, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(room_id, agent_id) DO UPDATE SET "
                "last_ack_seq = MAX(last_ack_seq, excluded.last_ack_seq), updated_at = excluded.updated_at",
                (room_id, agent_id, int(seq), now),
            )
            row = conn.execute(
                "SELECT last_ack_seq FROM room_cursors WHERE room_id = ? AND agent_id = ?",
                (room_id, agent_id),
            ).fetchone()
            return {"room_id": room_id, "agent_id": agent_id, "last_ack_seq": int(row["last_ack_seq"])}

    # ------------------------------------------------------------------
    # Addressing — compose roster route_targets + envelope_v2 + outbox
    # ------------------------------------------------------------------

    def room_send(self, team_id: str, room_id: str, sender_agent_id: str, target_spec: Any,
                  payload: Any, actor_token: str, exclude_sender: bool = True,
                  message_kind: str | None = None) -> dict[str, Any]:
        import weft_mcp.roster as _roster
        import weft_mcp.outbox as _outbox
        message_kind = _validate_message_kind(message_kind)
        with self._transaction() as conn:
            room = self._require_authenticated_member(conn, team_id, room_id, sender_agent_id, actor_token)
            if room["state"] == "closed":
                raise RoomError("room_closed", "Room is closed")
            targets = self._route_targets(conn, room_id, target_spec)
            self._reject_unroutable_specs(conn, room_id, target_spec, targets)
            if exclude_sender and sender_agent_id in targets:
                targets = [t for t in targets if t != sender_agent_id]
            seq = self._append_event(conn, room_id, sender_agent_id, "room.message",
                                     {"payload": payload, "target_spec": target_spec},
                                     message_kind=message_kind)
        envelope = _roster.build_envelope_v2(sender_agent_id, targets, "room.message",
                                             payload, capabilities=None)
        envelope["envelope_id"] = _new_id("oev")
        # Attach per-target idempotency keys for the receipts surface.
        per_target_keys = {}
        for t in envelope.get("per_target", []):
            recipient = t["recipient"]["agent_id"]
            per_target_keys[recipient] = t["idempotency_key"]
        envelope["per_target_idempotency_keys"] = per_target_keys
        entry_ids = _outbox.enqueue(envelope, targets, roster_or_team_id=room_id)
        receipts = []
        for agent_id, entry_id in zip(targets, entry_ids):
            receipts.append({"agent_id": agent_id, "entry_id": entry_id, "status": "queued"})
        return {"room_id": room_id, "envelope": envelope, "receipts": receipts, "seq": seq}

    def _route_targets(self, conn: sqlite3.Connection, room_id: str, target_spec: Any) -> list[str]:
        """Expand a target spec over the room's OWN member set.

        Deliverability is keyed on MEMBERSHIP, not on current presence.
        ``stale`` is a derived presence notion (idle longer than the presence
        window); an idle member still holds a seat and must still receive its
        mail — the event log IS the delivery mechanism and the recipient reads
        it when it returns. Only ``left`` members and non-members are
        unroutable.

        This is the P0 fix: routing previously dropped idle members, so a
        unicast to one created NO delivery row and NO receipt, and the message
        was silently lost to its own intended recipient.
        """
        member_rows = conn.execute(
            "SELECT agent_id FROM room_members WHERE room_id = ? AND status = 'active'",
            (room_id,),
        ).fetchall()
        active_ids: set[str] = {row["agent_id"] for row in member_rows}

        group_rows = conn.execute(
            "SELECT group_name, agent_id FROM room_group_members WHERE room_id = ?",
            (room_id,),
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

    def _reject_unroutable_specs(self, conn: sqlite3.Connection, room_id: str,
                                 target_spec: Any, routed: list[str]) -> None:
        """Refuse a send that names a target that is not a current member.

        NO SILENT SUCCESS: ``200`` with ``receipts: []`` was indistinguishable
        from a successful send. A spec that names an agent (a bare string that
        is neither ``"*"`` nor a group name) must resolve to a current member
        or the whole send is refused with ``recipient_not_found``. ``left``
        members and never-joined ids both hit this: they are genuinely
        undeliverable.
        """
        if isinstance(target_spec, str):
            specs = [target_spec]
        else:
            specs = list(target_spec or [])
        group_rows = conn.execute(
            "SELECT group_name FROM room_groups WHERE room_id = ?", (room_id,),
        ).fetchall()
        group_names = {row["group_name"] for row in group_rows}
        unrouted = [spec for spec in specs
                    if isinstance(spec, str) and spec != "*"
                    and spec not in routed and spec not in group_names]
        if unrouted:
            raise RoomError(
                "recipient_not_found",
                f"Recipient(s) are not members of this room: {', '.join(unrouted)}",
            )

    def receipts(self, team_id: str, room_id: str, agent_id: str, entry_ids: Sequence[str],
                 actor_token: str) -> dict[str, Any]:
        import weft_mcp.outbox as _outbox
        with self._transaction() as conn:
            self._require_authenticated_member(conn, team_id, room_id, agent_id, actor_token)
        result = []
        for eid in entry_ids:
            entry = _outbox.get_entry(eid)
            if entry is None:
                result.append({"entry_id": eid, "status": "not_found", "attempts": 0,
                               "next_attempt_at": None, "last_error": None})
            else:
                result.append({
                    "entry_id": entry.get("entry_id"),
                    "status": entry.get("status"),
                    "attempts": entry.get("attempts", 0),
                    "next_attempt_at": entry.get("next_attempt_at"),
                    "last_error": entry.get("last_error"),
                })
        return {"receipts": result}

    # ------------------------------------------------------------------
    # Groups — wrap roster group ops scoped to the room
    # ------------------------------------------------------------------

    def groups(self, team_id: str, room_id: str, agent_id: str, group_name: str,
               action: str, actor_token: str, members: Sequence[str] | None = None) -> dict[str, Any]:
        _validate_id(group_name, "group_name")
        with self._transaction() as conn:
            self._require_authenticated_member(conn, team_id, room_id, agent_id, actor_token)
            if action == "add":
                for member in members or []:
                    member_row = conn.execute(
                        "SELECT 1 FROM room_members WHERE room_id = ? AND agent_id = ? AND status = 'active'",
                        (room_id, member),
                    ).fetchone()
                    if member_row is None:
                        raise RoomError("member_required", f"Agent '{member}' is not an active member of this room")
                    conn.execute(
                        "INSERT OR IGNORE INTO room_groups(room_id, group_name) VALUES (?, ?)",
                        (room_id, group_name),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO room_group_members(room_id, group_name, agent_id) VALUES (?, ?, ?)",
                        (room_id, group_name, member),
                    )
            elif action == "remove":
                for member in members or []:
                    conn.execute(
                        "DELETE FROM room_group_members WHERE room_id = ? AND group_name = ? AND agent_id = ?",
                        (room_id, group_name, member),
                    )
            elif action != "list":
                raise RoomError("invalid_argument", "action must be add, remove, or list")
            rows = conn.execute(
                "SELECT agent_id FROM room_group_members WHERE room_id = ? AND group_name = ? ORDER BY agent_id",
                (room_id, group_name),
            ).fetchall()
            member_list = [r["agent_id"] for r in rows]
        return {"room_id": room_id, "group_name": group_name, "members": member_list}
