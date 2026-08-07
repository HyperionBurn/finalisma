"""Weft N-way roster layer.

Extends the core store with roster-scoped multi-agent groups, group
routing, one-use invite links, and a v2 multi-recipient envelope. Runs
in the same SQLite database as the core store; tables are prefixed
``roster_`` to avoid collisions.

Stdlib only. No shell execution from payloads.
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

SCHEMA_VERSION = 1
STALE_AFTER_SECONDS = 1800  # mirrors core's default heartbeat timeout


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


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS roster_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roster_members (
    roster_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    last_seen REAL NOT NULL,
    status TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    PRIMARY KEY (roster_id, agent_id)
);
CREATE TABLE IF NOT EXISTS roster_groups (
    roster_id TEXT NOT NULL,
    group_name TEXT NOT NULL,
    PRIMARY KEY (roster_id, group_name)
);
CREATE TABLE IF NOT EXISTS roster_group_members (
    group_name TEXT NOT NULL,
    roster_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    PRIMARY KEY (group_name, roster_id, agent_id)
);
CREATE TABLE IF NOT EXISTS roster_links (
    link_id TEXT PRIMARY KEY,
    roster_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at TEXT,
    consumed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_roster_links_roster
    ON roster_links(roster_id, expires_at);
"""


class RosterStore:
    """SQLite-backed N-way roster state, coexisting with the core store."""

    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(Path(db_path).expanduser().resolve())
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
            connection.executescript(_SCHEMA_SQL)
            connection.execute(
                "INSERT INTO roster_meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Roster lifecycle
    # ------------------------------------------------------------------

    def create_roster(self, owner_agent_id: str, team_id: str) -> str:
        _validate_id(owner_agent_id, "owner_agent_id")
        roster_id = _new_id("roster")
        now = _utc_now()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO roster_members(roster_id, agent_id, joined_at, last_seen, status, capabilities_json) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                (roster_id, owner_agent_id, now, _epoch(), _json([])),
            )
        return roster_id

    def join_roster(self, roster_id: str, agent_id: str, capabilities_json: Sequence[str] | None = None) -> None:
        _validate_id(agent_id, "agent_id")
        caps = list(capabilities_json or [])
        now = _utc_now()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO roster_members(roster_id, agent_id, joined_at, last_seen, status, capabilities_json)
                VALUES (?, ?, ?, ?, 'active', ?)
                ON CONFLICT(roster_id, agent_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    status = 'active',
                    capabilities_json = excluded.capabilities_json
                """,
                (roster_id, agent_id, now, _epoch(), _json(caps)),
            )

    def leave_roster(self, roster_id: str, agent_id: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM roster_members WHERE roster_id = ? AND agent_id = ?",
                (roster_id, agent_id),
            )
            conn.execute(
                "DELETE FROM roster_group_members WHERE roster_id = ? AND agent_id = ?",
                (roster_id, agent_id),
            )

    def list_members(self, roster_id: str) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM roster_members WHERE roster_id = ? ORDER BY joined_at",
                (roster_id,),
            ).fetchall()
        return [self._member_dict(dict(row)) for row in rows]

    @staticmethod
    def _member_dict(row: dict[str, Any]) -> dict[str, Any]:
        age = max(0.0, _epoch() - float(row["last_seen"]))
        stale = age > STALE_AFTER_SECONDS
        return {
            "agent_id": row["agent_id"],
            "joined_at": row["joined_at"],
            "last_seen": row["last_seen"],
            "age_seconds": round(age, 3),
            "status": "stale" if stale else row["status"],
            "capabilities": _parse_json(row["capabilities_json"], []),
        }

    def member_status(self, roster_id: str, agent_id: str) -> str:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM roster_members WHERE roster_id = ? AND agent_id = ?",
                (roster_id, agent_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Agent '{agent_id}' is not a member of roster '{roster_id}'")
        return self._member_dict(dict(row))["status"]

    def heartbeat(self, roster_id: str, agent_id: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE roster_members SET last_seen = ?, status = 'active' "
                "WHERE roster_id = ? AND agent_id = ?",
                (_epoch(), roster_id, agent_id),
            )

    def set_capabilities(self, roster_id: str, agent_id: str, capabilities_json: Sequence[str]) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE roster_members SET capabilities_json = ? "
                "WHERE roster_id = ? AND agent_id = ?",
                (_json(list(capabilities_json)), roster_id, agent_id),
            )

    def get_capabilities(self, roster_id: str, agent_id: str) -> list[str]:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT capabilities_json FROM roster_members WHERE roster_id = ? AND agent_id = ?",
                (roster_id, agent_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Agent '{agent_id}' is not a member of roster '{roster_id}'")
        return _parse_json(row["capabilities_json"], [])

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def add_to_group(self, roster_id: str, group_name: str, agent_id: str) -> None:
        _validate_id(group_name, "group_name")
        with self._transaction() as conn:
            # Ensure membership exists.
            member = conn.execute(
                "SELECT 1 FROM roster_members WHERE roster_id = ? AND agent_id = ?",
                (roster_id, agent_id),
            ).fetchone()
            if member is None:
                raise ValueError(f"Agent '{agent_id}' is not in roster '{roster_id}'")
            conn.execute(
                "INSERT OR IGNORE INTO roster_groups(roster_id, group_name) VALUES (?, ?)",
                (roster_id, group_name),
            )
            conn.execute(
                "INSERT OR IGNORE INTO roster_group_members(group_name, roster_id, agent_id) VALUES (?, ?, ?)",
                (group_name, roster_id, agent_id),
            )

    def remove_from_group(self, roster_id: str, group_name: str, agent_id: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM roster_group_members WHERE group_name = ? AND roster_id = ? AND agent_id = ?",
                (group_name, roster_id, agent_id),
            )

    def list_group(self, roster_id: str, group_name: str) -> list[str]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT agent_id FROM roster_group_members WHERE roster_id = ? AND group_name = ?",
                (roster_id, group_name),
            ).fetchall()
        return [row["agent_id"] for row in rows]

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def route_targets(self, roster_id: str, target_spec: str | Sequence[str]) -> list[str]:
        """Expand a target spec into a de-duplicated, status-filtered recipient list.

        ``target_spec`` may be:
        - a single agent_id string
        - a group name string
        - ``"*"`` for broadcast to all active members
        - a list of any of the above (mixed)

        Stale members are excluded. The sender is NOT auto-excluded — the
        caller filters if needed.
        """
        with self._transaction() as conn:
            # Preload active members and group map.
            member_rows = conn.execute(
                "SELECT agent_id, last_seen FROM roster_members WHERE roster_id = ?",
                (roster_id,),
            ).fetchall()
            active_ids: set[str] = set()
            for row in member_rows:
                age = max(0.0, _epoch() - float(row["last_seen"]))
                if age <= STALE_AFTER_SECONDS:
                    active_ids.add(row["agent_id"])

            group_rows = conn.execute(
                "SELECT group_name, agent_id FROM roster_group_members WHERE roster_id = ?",
                (roster_id,),
            ).fetchall()
            groups: dict[str, set[str]] = {}
            for row in group_rows:
                groups.setdefault(row["group_name"], set()).add(row["agent_id"])

        def _expand(spec: str) -> set[str]:
            if spec == "*":
                return set(active_ids)
            if spec in groups:
                return groups[spec] & active_ids
            # Treat as agent_id if active.
            if spec in active_ids:
                return {spec}
            return set()

        if isinstance(target_spec, str):
            specs = [target_spec]
        else:
            specs = list(target_spec)

        result: set[str] = set()
        for spec in specs:
            result |= _expand(spec)
        return sorted(result)

    # ------------------------------------------------------------------
    # Invite links (one-use, opaque, expiring)
    # ------------------------------------------------------------------

    def create_link(
        self,
        roster_id: str,
        created_by: str,
        ttl_seconds: int = 3600,
    ) -> str:
        _validate_id(created_by, "created_by")
        raw_token = f"rst_{secrets.token_urlsafe(32)}"
        link_id = _new_id("link")
        token_hash = _token_hash(raw_token)
        now = _utc_now()
        expires_at = _epoch() + max(1, ttl_seconds)
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO roster_links(link_id, roster_id, token_hash, created_by, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (link_id, roster_id, token_hash, created_by, now, expires_at),
            )
        # Return composite: link_id + raw token. Caller shares the token
        # out-of-band; the store only keeps the hash.
        return f"{raw_token}"

    def consume_link(self, roster_id: str, raw_token: str) -> bool:
        """Attempt to consume a link. Returns True on success, False if
        invalid, expired, or already used (one-use semantics)."""
        try:
            token_hash = _token_hash(raw_token)
        except ValueError:
            return False
        now = _epoch()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM roster_links WHERE token_hash = ? AND roster_id = ?",
                (token_hash, roster_id),
            ).fetchone()
            if row is None:
                return False
            if row["consumed_at"] is not None:
                return False
            if float(row["expires_at"]) < now:
                return False
            conn.execute(
                "UPDATE roster_links SET consumed_at = ? WHERE link_id = ?",
                (_utc_now(), row["link_id"]),
            )
            return True

    # ------------------------------------------------------------------
    # v2 multi-recipient envelope
    # ------------------------------------------------------------------

    def build_envelope_v2(
        self,
        sender: str,
        targets: Sequence[str],
        type: str,
        payload: Any,
        capabilities: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Assemble a v2 multi-recipient envelope.

        Each target gets its own idempotency key so the same logical
        message can be fanned out without cross-recipient collisions.
        A single correlation_id binds the fan-out; a trace_id enables
        distributed tracing.
        """
        correlation_id = _new_id("corr")
        trace_id = _new_id("trace")
        timestamp = _utc_now()
        per_target = []
        for tgt in targets:
            # Per-target key = hash(sender + target + correlation + trace).
            digest = hashlib.sha256(
                f"{sender}|{tgt}|{correlation_id}|{trace_id}".encode("utf-8")
            ).hexdigest()[:32]
            per_target.append({
                "recipient": {"agent_id": tgt},
                "idempotency_key": f"idem_{digest}",
            })
        return {
            "protocol": "finalisma.a2a",
            "version": "2.0",
            "message_id": _new_id("msg"),
            "type": type,
            "sender": {"agent_id": sender},
            "targets": list(targets),
            "timestamp": timestamp,
            "correlation_id": correlation_id,
            "trace_id": trace_id,
            "payload": payload,
            "capabilities": list(capabilities or []),
            "per_target": per_target,
        }

    # ------------------------------------------------------------------
    # Test/ops helper: back-date a member's heartbeat
    # ------------------------------------------------------------------

    def _backdate_heartbeat(self, roster_id: str, agent_id: str, seconds: float) -> None:
        """Force a member's last_seen into the past (test helper)."""
        with self._transaction() as conn:
            conn.execute(
                "UPDATE roster_members SET last_seen = ? WHERE roster_id = ? AND agent_id = ?",
                (_epoch() - seconds, roster_id, agent_id),
            )


# ---------------------------------------------------------------------------
# Module-level convenience API (stateless, opens its own connection)
# ---------------------------------------------------------------------------

_store: RosterStore | None = None


def init(db_path: str | os.PathLike[str]) -> None:
    """Idempotent: initialize the roster schema in the given SQLite file."""
    global _store
    _store = RosterStore(str(db_path))


def _s() -> RosterStore:
    if _store is None:
        raise RuntimeError("roster.init(db_path) must be called first")
    return _store


def create_roster(owner_agent_id: str, team_id: str) -> str:
    return _s().create_roster(owner_agent_id, team_id)


def join_roster(roster_id: str, agent_id: str, capabilities_json: Sequence[str] | None = None) -> None:
    _s().join_roster(roster_id, agent_id, capabilities_json)


def leave_roster(roster_id: str, agent_id: str) -> None:
    _s().leave_roster(roster_id, agent_id)


def list_members(roster_id: str) -> list[dict[str, Any]]:
    return _s().list_members(roster_id)


def member_status(roster_id: str, agent_id: str) -> str:
    return _s().member_status(roster_id, agent_id)


def heartbeat(roster_id: str, agent_id: str) -> None:
    _s().heartbeat(roster_id, agent_id)


def set_capabilities(roster_id: str, agent_id: str, capabilities_json: Sequence[str]) -> None:
    _s().set_capabilities(roster_id, agent_id, capabilities_json)


def get_capabilities(roster_id: str, agent_id: str) -> list[str]:
    return _s().get_capabilities(roster_id, agent_id)


def add_to_group(roster_id: str, group_name: str, agent_id: str) -> None:
    _s().add_to_group(roster_id, group_name, agent_id)


def remove_from_group(roster_id: str, group_name: str, agent_id: str) -> None:
    _s().remove_from_group(roster_id, group_name, agent_id)


def list_group(roster_id: str, group_name: str) -> list[str]:
    return _s().list_group(roster_id, group_name)


def route_targets(roster_id: str, target_spec: str | Sequence[str]) -> list[str]:
    return _s().route_targets(roster_id, target_spec)


def create_link(roster_id: str, created_by: str, ttl_seconds: int = 3600) -> str:
    return _s().create_link(roster_id, created_by, ttl_seconds)


def consume_link(roster_id: str, raw_token: str) -> bool:
    return _s().consume_link(roster_id, raw_token)


def build_envelope_v2(
    sender: str,
    targets: Sequence[str],
    type: str,
    payload: Any,
    capabilities: Sequence[str] | None = None,
) -> dict[str, Any]:
    return _s().build_envelope_v2(sender, targets, type, payload, capabilities)


def _backdate_heartbeat(roster_id: str, agent_id: str, seconds: float) -> None:
    """Test helper: force a member's heartbeat into the past."""
    _s()._backdate_heartbeat(roster_id, agent_id, seconds)
