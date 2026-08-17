"""Activation funnel metrics for Weft.

Local-first, stdlib-only, SQLite-backed. No external analytics vendor, no PII.

The activation funnel:
    link_created → link_previewed → link_accepted → first_task_claimed → first_evidence_verified

Headline metric: time-to-first-verified-handoff (ttfvh_ms), measured from
link_created_at to first_evidence_verified_at for the same workspace.

Integration seam:
    The orchestrator is expected to call ``init(db_path)`` at server startup
    and route store events through ``record_from_store_event(store_event)``.
    This module does NOT wire itself — wiring is the orchestrator's job.
"""

from __future__ import annotations

import datetime as dt
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

# Mapping from core store event types → activation event types.
# The orchestrator routes store events through record_from_store_event().
_STORE_EVENT_MAP: dict[str, str] = {
    "pairing.created": "link_created",
    "pairing.previewed": "link_previewed",
    "pairing.accepted": "link_accepted",
    "task.claimed": "first_task_claimed",
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
            connection.executescript(_SCHEMA)
        finally:
            connection.close()


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
        connection.execute(
            "INSERT OR IGNORE INTO metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, team_id, agent_id, event_type, occurred_at, json.dumps(metadata, sort_keys=True)),
        )
        connection.commit()
    finally:
        connection.close()

    return event_id


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


def record_from_store_event(store_event: dict[str, Any]) -> str | None:
    """Integration seam: convert a core store event into an activation event.

    The orchestrator is responsible for calling this function when relevant
    store events are persisted. This module does NOT wire itself.

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
    agent_id = store_event.get("actor_id") or store_event.get("object_id") or ""
    payload = store_event.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}

    # Derive a deterministic event_id from the store event so re-wiring
    # the same event is idempotent.
    import hashlib
    seed = f"{team_id}:{agent_id}:{event_type}:{store_event.get('object_id', '')}"
    event_id = "mevt_store_" + hashlib.sha256(seed.encode()).hexdigest()[:24]

    return record_event(
        team_id=team_id,
        agent_id=agent_id,
        event_type=activation_type,
        metadata=payload if isinstance(payload, dict) else {},
        event_id=event_id,
    )
