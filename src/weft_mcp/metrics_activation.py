"""Activation funnel metrics for Weft.

Local-first, stdlib-only, SQLite-backed. No external analytics vendor, no PII.

The activation funnel:
    link_created → link_previewed → link_accepted → first_task_claimed → first_evidence_verified

Headline metric: time-to-first-verified-handoff (ttfvh_ms), measured from
link_created_at to first_evidence_verified_at for the same workspace.

Integration seam:
    ``WeftDispatcher`` calls ``init(db_path)`` at startup and attaches a
    same-connection observer to the core store. Direct callers can still use
    ``record_from_store_event(store_event)`` for an explicit integration seam.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import threading
from typing import Any

# ---------------------------------------------------------------------------
# Activation event types
# ---------------------------------------------------------------------------

ACTIVATION_EVENTS = (
    "link_created",
    "link_previewed",
    "link_accepted",
    "first_task_claimed",
    "first_evidence_verified",
)

# Mapping from core store event types → activation event types. The legacy
# names remain accepted for callers that used the original integration seam.
_STORE_EVENT_MAP: dict[str, str] = {
    "pairing.issued": "link_created",
    "pairing.created": "link_created",
    "pairing.previewed": "link_previewed",
    "pairing.joined": "link_accepted",
    "pairing.accepted": "link_accepted",
    "task.claimed": "first_task_claimed",
    "quality.evaluated": "first_evidence_verified",
    "task.verified": "first_evidence_verified",
}

# Module-level lock for connection lifecycle.
_lock = threading.Lock()
_db_path: str | None = None


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=15, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics_events (
    event_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_events_team ON metrics_events(team_id, event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_metrics_events_workspace ON metrics_events(team_id, event_type, metadata_json);

CREATE TABLE IF NOT EXISTS metrics_funnels (
    team_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    link_created_at TEXT,
    link_previewed_at TEXT,
    link_accepted_at TEXT,
    first_task_claimed_at TEXT,
    first_evidence_verified_at TEXT,
    ttfvh_ms INTEGER,
    PRIMARY KEY (team_id, workspace_id)
);
"""


def init(db_path: str | Any) -> None:
    """Idempotent schema initialization.

    Creates ``metrics_events`` and ``metrics_funnels`` tables in the same
    SQLite file as the core store. Safe to call at every server startup.
    """
    global _db_path
    with _lock:
        _db_path = str(db_path)
        connection = _connect(_db_path)
        try:
            ensure_schema(connection)
        finally:
            connection.close()


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the metrics tables on an existing SQLite connection."""
    connection.executescript(_SCHEMA)


def _connection() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("metrics_activation.init() must be called before use")
    return _connect(_db_path)


def _workspace_id_from_metadata(metadata_json: str) -> str | None:
    try:
        meta = json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(meta, dict):
        return meta.get("workspace_id")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_event(
    team_id: str,
    agent_id: str,
    event_type: str,
    metadata: dict[str, Any] | None = None,
    *,
    event_id: str | None = None,
) -> str:
    """Record an activation event.

    Args:
        team_id: the team scope (required).
        agent_id: the agent that triggered the event (required).
        event_type: one of the ACTIVATION_EVENTS constants.
        metadata: caller-controlled dict. MUST NOT contain PII. Commonly
            carries ``{"workspace_id": "..."}`` to tie the event to a funnel.
        event_id: optional explicit id (used by the integration seam for
            idempotency). If omitted, a deterministic id is derived from
            (team_id, agent_id, event_type, occurred_at).

    Returns:
        The event_id of the inserted row.
    """
    if event_type not in ACTIVATION_EVENTS:
        raise ValueError(f"Unknown activation event type: {event_type!r}")

    metadata = metadata or {}
    # No-PII enforcement: metadata must be a dict and must not contain
    # obviously sensitive keys at the top level.
    _assert_no_pii(metadata)

    occurred_at = _utc_now()
    if event_id is None:
        # Deterministic-ish id: caller-controlled for idempotency.
        import hashlib
        import uuid
        seed = f"{team_id}:{agent_id}:{event_type}:{occurred_at}:{uuid.uuid4().hex}"
        event_id = "mevt_" + hashlib.sha256(seed.encode()).hexdigest()[:24]

    connection = _connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = _record_event_on_connection(
            connection,
            team_id,
            agent_id,
            event_type,
            metadata,
            event_id=event_id,
            occurred_at=occurred_at,
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _record_event_on_connection(
    connection: sqlite3.Connection,
    team_id: str,
    agent_id: str,
    event_type: str,
    metadata: dict[str, Any],
    *,
    event_id: str,
    occurred_at: str,
) -> str:
    """Insert an event without opening or committing a nested transaction."""
    if event_type not in ACTIVATION_EVENTS:
        raise ValueError(f"Unknown activation event type: {event_type!r}")
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    _assert_no_pii(metadata)
    connection.execute(
        "INSERT OR IGNORE INTO metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, team_id, agent_id, event_type, occurred_at, json.dumps(metadata, sort_keys=True, separators=(",", ":"))),
    )
    workspace_id = metadata.get("workspace_id")
    if isinstance(workspace_id, str) and workspace_id:
        _refresh_funnel(connection, team_id, workspace_id)
    return event_id


def _refresh_funnel(connection: sqlite3.Connection, team_id: str, workspace_id: str) -> None:
    """Refresh the materialized funnel row for one workspace."""
    row = connection.execute(
        """
        SELECT
            MIN(CASE WHEN event_type = 'link_created' THEN occurred_at END) AS link_created_at,
            MIN(CASE WHEN event_type = 'link_previewed' THEN occurred_at END) AS link_previewed_at,
            MIN(CASE WHEN event_type = 'link_accepted' THEN occurred_at END) AS link_accepted_at,
            MIN(CASE WHEN event_type = 'first_task_claimed' THEN occurred_at END) AS first_task_claimed_at,
            MIN(CASE WHEN event_type = 'first_evidence_verified' THEN occurred_at END) AS first_evidence_verified_at
        FROM metrics_events
        WHERE team_id = ? AND json_extract(metadata_json, '$.workspace_id') = ?
        """,
        (team_id, workspace_id),
    ).fetchone()
    if row is None:
        return
    ttfvh_ms: int | None = None
    if row["link_created_at"] and row["first_evidence_verified_at"]:
        created = _parse_iso(row["link_created_at"])
        verified = _parse_iso(row["first_evidence_verified_at"])
        if created is not None and verified is not None:
            ttfvh_ms = max(0, int((verified - created).total_seconds() * 1000))
    connection.execute(
        """
        INSERT INTO metrics_funnels(
            team_id, workspace_id, link_created_at, link_previewed_at,
            link_accepted_at, first_task_claimed_at,
            first_evidence_verified_at, ttfvh_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, workspace_id) DO UPDATE SET
            link_created_at = excluded.link_created_at,
            link_previewed_at = excluded.link_previewed_at,
            link_accepted_at = excluded.link_accepted_at,
            first_task_claimed_at = excluded.first_task_claimed_at,
            first_evidence_verified_at = excluded.first_evidence_verified_at,
            ttfvh_ms = excluded.ttfvh_ms
        """,
        (
            team_id,
            workspace_id,
            row["link_created_at"],
            row["link_previewed_at"],
            row["link_accepted_at"],
            row["first_task_claimed_at"],
            row["first_evidence_verified_at"],
            ttfvh_ms,
        ),
    )


def _assert_no_pii(metadata: dict[str, Any]) -> None:
    """Reject metadata that carries obviously PII-bearing top-level keys.

    This is a best-effort guard, not a substitute for caller discipline.
    Documented as a contract: callers MUST NOT put emails, IPs, names,
    phone numbers, or user-agent strings into metadata_json.
    """
    forbidden = {"email", "ip_address", "ip", "name", "phone", "user_agent", "geo", "location"}
    for key in metadata:
        if key.lower() in forbidden:
            raise ValueError(f"metadata must not contain PII key: {key!r}")


def derive_ttfvh(team_id: str, workspace_id: str) -> int | None:
    """Compute time-to-first-verified-handoff in milliseconds.

    Returns ``None`` if the workspace has not yet reached the
    ``first_evidence_verified`` stage.
    """
    connection = _connection()
    try:
        row = connection.execute(
            """
            SELECT
                MIN(CASE WHEN event_type = 'link_created' THEN occurred_at END) AS link_created_at,
                MIN(CASE WHEN event_type = 'first_evidence_verified' THEN occurred_at END) AS first_evidence_verified_at
            FROM metrics_events
            WHERE team_id = ? AND json_extract(metadata_json, '$.workspace_id') = ?
            """,
            (team_id, workspace_id),
        ).fetchone()
    finally:
        connection.close()

    if row is None or row["link_created_at"] is None or row["first_evidence_verified_at"] is None:
        return None

    created = _parse_iso(row["link_created_at"])
    verified = _parse_iso(row["first_evidence_verified_at"])
    if created is None or verified is None:
        return None

    delta = (verified - created).total_seconds() * 1000
    return max(0, int(delta))


def _parse_iso(value: str) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp into a UTC datetime."""
    if not value:
        return None
    # Handle both 'Z' and '+00:00' suffixes
    value = value.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def funnel_snapshot(team_id: str) -> dict[str, Any]:
    """Per-stage counts and funnel deltas for a team.

    Returns:
        ``{"stages": {...}, "deltas": {...}}`` where stages maps each
        activation event type to its count, and deltas maps transition
        names (e.g. ``"preview_to_accept"``) to conversion ratios.
    """
    connection = _connection()
    try:
        rows = connection.execute(
            "SELECT event_type, COUNT(*) AS c FROM metrics_events WHERE team_id = ? GROUP BY event_type",
            (team_id,),
        ).fetchall()
    finally:
        connection.close()

    stages: dict[str, int] = {event: 0 for event in ACTIVATION_EVENTS}
    for row in rows:
        if row["event_type"] in stages:
            stages[row["event_type"]] = row["c"]

    deltas = _compute_deltas(stages)
    return {"stages": stages, "deltas": deltas}


def _compute_deltas(stages: dict[str, int]) -> dict[str, float]:
    def ratio(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator > 0 else 0.0

    return {
        "preview_to_accept": ratio(stages["link_accepted"], stages["link_previewed"]),
        "accept_to_claim": ratio(stages["first_task_claimed"], stages["link_accepted"]),
        "claim_to_verify": ratio(stages["first_evidence_verified"], stages["first_task_claimed"]),
        "overall": ratio(stages["first_evidence_verified"], stages["link_created"]),
    }


def weekly_retention(team_id: str, week_start: str) -> dict[str, Any]:
    """Workspaces with >= 1 evidence-gated handoff in the given week.

    Args:
        team_id: the team scope.
        week_start: ISO-8601 date (e.g. ``"2026-01-05"``) for the Monday
            of the target week. The window is [week_start, week_start + 7 days).

    Returns:
        ``{"retained_workspaces": int, "workspace_ids": [...]}``
    """
    start = _parse_iso(week_start)
    if start is None:
        raise ValueError(f"Invalid week_start: {week_start!r}")
    # Normalize to midnight UTC
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=7)

    connection = _connection()
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT json_extract(metadata_json, '$.workspace_id') AS workspace_id
            FROM metrics_events
            WHERE team_id = ?
              AND event_type = 'first_evidence_verified'
              AND occurred_at >= ? AND occurred_at < ?
              AND json_extract(metadata_json, '$.workspace_id') IS NOT NULL
            ORDER BY workspace_id
            """,
            (team_id, start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")),
        ).fetchall()
    finally:
        connection.close()

    workspace_ids = [row["workspace_id"] for row in rows]
    return {"retained_workspaces": len(workspace_ids), "workspace_ids": workspace_ids}


def handoffs_per_workspace(team_id: str) -> dict[str, Any]:
    """Evidence-verified handoffs per active workspace.

    Returns:
        ``{"active_workspaces": int, "total_handoffs": int, "handoffs_per_workspace_mean": float}``
    """
    connection = _connection()
    try:
        rows = connection.execute(
            """
            SELECT json_extract(metadata_json, '$.workspace_id') AS workspace_id, COUNT(*) AS c
            FROM metrics_events
            WHERE team_id = ? AND event_type = 'first_evidence_verified'
              AND json_extract(metadata_json, '$.workspace_id') IS NOT NULL
            GROUP BY workspace_id
            """,
            (team_id,),
        ).fetchall()
    finally:
        connection.close()

    counts = [row["c"] for row in rows]
    total = sum(counts)
    n = len(counts)
    mean = total / n if n > 0 else 0.0
    return {"active_workspaces": n, "total_handoffs": total, "handoffs_per_workspace_mean": round(mean, 2)}


def invite_to_activated_conversion(team_id: str) -> dict[str, Any]:
    """Ratio of invites (link_created) that reached evidence-verified.

    Returns:
        ``{"invites": int, "activated": int, "conversion_rate": float}``
    """
    connection = _connection()
    try:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN event_type = 'first_evidence_verified' THEN 1 ELSE 0 END) AS activated
            FROM metrics_events
            WHERE team_id = ? AND event_type IN ('link_created', 'first_evidence_verified')
            """,
            (team_id,),
        ).fetchone()
    finally:
        connection.close()

    invites = row["total"] - row["activated"] if row["activated"] else row["total"]
    # More precise: count link_created separately
    connection = _connection()
    try:
        row_created = connection.execute(
            "SELECT COUNT(*) AS c FROM metrics_events WHERE team_id = ? AND event_type = 'link_created'",
            (team_id,),
        ).fetchone()
        row_verified = connection.execute(
            "SELECT COUNT(*) AS c FROM metrics_events WHERE team_id = ? AND event_type = 'first_evidence_verified'",
            (team_id,),
        ).fetchone()
    finally:
        connection.close()

    invites = row_created["c"]
    activated = row_verified["c"]
    rate = activated / invites if invites > 0 else 0.0
    return {"invites": invites, "activated": activated, "conversion_rate": round(rate, 4)}


def record_from_store_event(
    store_event: dict[str, Any],
    *,
    connection: sqlite3.Connection | None = None,
) -> str | None:
    """Integration seam: convert a core store event into an activation event.

    ``WeftDispatcher`` calls this function with the core transaction's
    connection. Without that connection, this function opens its own small
    transaction for compatibility with direct callers.

    Args:
        store_event: a dict with keys ``event_type``, ``team_id``,
            ``actor_id``, ``object_id``, ``payload`` (as produced by
            ``WeftStore._insert_event``).

    Returns:
        The recorded activation event_id, or ``None`` if the store event
        type is not mapped to an activation event.
    """
    event_type = store_event.get("event_type", "")
    activation_type = _STORE_EVENT_MAP.get(event_type)
    if activation_type is None:
        return None

    team_id = store_event.get("team_id", "")
    agent_id = store_event.get("actor_id") or "system"
    payload = store_event.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}

    # A failed quality evaluation is not evidence of a verified handoff.
    if event_type == "quality.evaluated" and payload.get("status") != "passed":
        return None

    workspace_id = payload.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id.strip() or len(workspace_id.strip()) > 160:
        workspace_id = f"team:{team_id}"
    else:
        workspace_id = workspace_id.strip()
    metadata = {
        "workspace_id": workspace_id,
        "source_event_type": event_type,
    }

    # Derive a deterministic event_id from the activation stage and source
    # object. Repeated previews, claims, or evaluations of the same object do
    # not inflate a "first" funnel stage. A short source hash supports
    # auditability without copying the source identifier or payload.
    source_event_id = store_event.get("event_id")
    if not isinstance(source_event_id, str) or not source_event_id:
        source_event_id = f"{event_type}:{store_event.get('object_id', '')}"
    metadata["source_event_hash"] = hashlib.sha256(source_event_id.encode("utf-8")).hexdigest()[:24]
    source_object_id = store_event.get("object_id")
    if not isinstance(source_object_id, str) or not source_object_id:
        source_object_id = source_event_id
    seed = f"{team_id}:{activation_type}:{source_object_id}"
    event_id = "mevt_store_" + hashlib.sha256(seed.encode()).hexdigest()[:24]
    occurred_at = store_event.get("created_at")
    if not isinstance(occurred_at, str) or not occurred_at:
        occurred_at = _utc_now()

    if connection is not None:
        return _record_event_on_connection(
            connection,
            team_id,
            agent_id,
            activation_type,
            metadata,
            event_id=event_id,
            occurred_at=occurred_at,
        )

    connection = _connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = _record_event_on_connection(
            connection,
            team_id,
            agent_id,
            activation_type,
            metadata,
            event_id=event_id,
            occurred_at=occurred_at,
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
