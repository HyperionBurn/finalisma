"""Transport-neutral Weft coordination core.

The core deliberately uses only Python's standard library.  MCP is an
adapter at the edge; the task registry, message envelope, leases, and quality
gate remain usable from stdio, HTTP, tests, or a future native client.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import fnmatch
import hashlib
import json
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import quote, urlsplit

WEFT_PROTOCOL = "weft.a2a"
WEFT_VERSION = "1.0"
MCP_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_MCP_VERSIONS = ("2025-11-25", "2024-11-05")
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_COMPARE_CHARS = 2_000
MAX_JSON_DEPTH = 64
SCHEMA_VERSION = 3
CONNECTION_POOL_SIZE = 4

MODEL_SLOTS: tuple[dict[str, Any], ...] = (
    {
        "id": "gpt-5.6-luna",
        "label": "5.6 Luna Light",
        "provider": "openai",
        "route": "native",
        "status": "host-configured",
        "capabilities": ["planning", "critique", "coding", "research"],
        "secret_ref": "provider-managed",
    },
    {
        "id": "qwencloud/qwen3.8-max-preview",
        "alias": "qwen3.8-max",
        "label": "Qwen 3.8 Max Preview",
        "provider": "qwencloud",
        "route": "opencodex",
        "status": "host-configured",
        "capabilities": ["coding", "research", "long-context"],
        "secret_ref": "QWEN_API_KEY",
    },
    {
        "id": "longcat/LongCat-2.0",
        "label": "LongCat 2.0",
        "provider": "LongCat",
        "route": "opencodex",
        "status": "host-configured",
        "capabilities": ["parallel-execution", "coding", "knowledge-work"],
        "secret_ref": "provider-managed",
    },
    {
        "id": "opencode-go/mimo-v2.5",
        "label": "OpenCode Go Mimo v2.5",
        "provider": "opencode-go",
        "route": "opencodex",
        "status": "host-configured",
        "capabilities": ["security-review", "coding", "reasoning", "knowledge-work"],
        "secret_ref": "provider-managed",
    },
)

ROUTE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "architect": ("architect", "architecture", "design", "plan", "tradeoff"),
    "research": ("research", "compare", "benchmark", "investigate", "analyze"),
    "coding": ("code", "implement", "function", "bug", "fix", "refactor", "api"),
    "testing": ("test", "coverage", "pytest", "unittest", "verify", "qa"),
    "documentation": ("document", "readme", "explain", "tutorial", "guide"),
    "security": ("security", "vulnerability", "audit", "cve", "injection"),
}

TASK_STATUSES = {
    "pending",
    "assigned",
    "in_progress",
    "review",
    "verified",
    "done",
    "blocked",
    "cancelled",
}

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
)


class WeftError(RuntimeError):
    """A safe, structured error returned to the MCP client."""

    def __init__(self, code: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _epoch() -> float:
    return time.time()


def _json(value: Any) -> str:
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise WeftError("invalid_json", f"Value exceeds the maximum JSON nesting depth of {MAX_JSON_DEPTH}")
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend((child, depth + 1) for child in current)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise WeftError("invalid_json", "Value must be JSON serializable and not deeply nested") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise WeftError("payload_too_large", f"Payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    return encoded


def _parse_json(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WeftError("corrupt_state", "Persisted JSON is invalid") from exc


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _validate_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise WeftError("invalid_id", f"{field} must be 1-128 safe identifier characters")
    return value


def _normalize_objective(title: str, description: str) -> str:
    text = f"{title}\n{description}".casefold()
    return " ".join(text.split())


def _hash_objective(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WeftError("invalid_argument", f"Expected integer between {minimum} and {maximum}")
    return value


class WeftStore:
    """SQLite-backed shared state for multiple independent MCP processes."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        workspace_path: str | os.PathLike[str],
        heartbeat_timeout: int = 1800,
        public_base_url: str | None = None,
        require_actor_auth: bool = False,
    ):
        if not isinstance(require_actor_auth, bool):
            raise WeftError("invalid_argument", "require_actor_auth must be a boolean")
        self.state_path = Path(state_path).expanduser().resolve()
        self.workspace = Path(workspace_path).expanduser().resolve()
        self.heartbeat_timeout = _safe_int(heartbeat_timeout, 1800, 30, 86_400)
        self.require_actor_auth = require_actor_auth
        configured_public_url = (public_base_url or os.environ.get("WEFT_PUBLIC_URL") or "http://127.0.0.1:8787").strip()
        self.public_base_url = self._normalize_public_base_url(configured_public_url)
        self._connection_pools: dict[bool, queue.LifoQueue[sqlite3.Connection]] = {
            False: queue.LifoQueue(maxsize=CONNECTION_POOL_SIZE),
            True: queue.LifoQueue(maxsize=CONNECTION_POOL_SIZE),
        }
        self._live_connections: set[sqlite3.Connection] = set()
        self._connection_pool_lock = threading.Lock()
        self._closed = False
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _normalize_public_base_url(value: str) -> str:
        """Validate the URL embedded in a bearer-capability pairing link."""
        if not isinstance(value, str) or not value or any(character.isspace() for character in value):
            raise WeftError("invalid_public_url", "public_base_url must be an absolute HTTP(S) URL")
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError as exc:
            raise WeftError("invalid_public_url", "public_base_url must be an absolute HTTP(S) URL") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
            raise WeftError("invalid_public_url", "public_base_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise WeftError("invalid_public_url", "public_base_url cannot contain credentials, a query, or a fragment")
        return value.rstrip("/")

    def _connect(self, *, query_only: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.state_path,
            timeout=15,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        # sqlite3.connect(timeout=15) installs the same 15-second busy handler.
        # WAL is database-persistent and is established once in _initialize.
        if query_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _acquire_connection(self, *, query_only: bool = False) -> sqlite3.Connection:
        with self._connection_pool_lock:
            if self._closed:
                raise RuntimeError("WeftStore is closed")
            try:
                connection = self._connection_pools[query_only].get_nowait()
            except queue.Empty:
                connection = self._connect(query_only=query_only)
            # Track EVERY in-flight connection so close() can force-close it.
            self._live_connections.add(connection)
            return connection

    def _release_connection(self, connection: sqlite3.Connection, *, query_only: bool = False) -> None:
        with self._connection_pool_lock:
            self._live_connections.discard(connection)
            if self._closed:
                connection.close()
                return
            try:
                self._connection_pools[query_only].put_nowait(connection)
            except queue.Full:
                connection.close()

    def close(self) -> None:
        """Close every SQLite connection (idle and in-flight) and reject
        subsequent operations. Closing in-flight connections matters on
        Windows: a checked-out connection left open keeps the SQLite file
        locked, which made tempdir teardown intermittently raise
        PermissionError.

        After closing all pooled/live connections, a fresh connection performs
        a WAL checkpoint (TRUNCATE) so the -wal/-shm side files are folded into
        the main database and their handles released. Checkpointing must happen
        AFTER all other connections are closed — otherwise SQLite reports BUSY
        and the side files linger, locking the file on Windows."""
        connections: list[sqlite3.Connection] = []
        with self._connection_pool_lock:
            if self._closed:
                return
            self._closed = True
            for pool in self._connection_pools.values():
                while True:
                    try:
                        connections.append(pool.get_nowait())
                    except queue.Empty:
                        break
            connections.extend(self._live_connections)
            self._live_connections.clear()
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        # Drop references so the closed connection objects (and any lingering
        # WAL -shm mapping on Windows) can be collected before we switch modes.
        connections.clear()
        # Windows holds WAL -shm mappings on the closed connection objects; a
        # GC pass releases them deterministically so the DELETE-mode switch
        # below can actually take the file and remove the side files.
        import gc as _gc
        _gc.collect()
        # Fresh connection: checkpoint WAL now that no other handle is open.
        try:
            checkpoint_conn = sqlite3.connect(self.state_path, timeout=5, isolation_level=None)
            try:
                checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                checkpoint_conn.close()
        except sqlite3.Error:
            pass
        # Return the database to DELETE journal mode so no WAL -wal/-shm side
        # files remain to lock the file on Windows teardown. A final connection
        # forces the mode switch and folds any residual WAL into the main file.
        try:
            final_conn = sqlite3.connect(self.state_path, timeout=5, isolation_level=None)
            try:
                final_conn.execute("PRAGMA journal_mode = DELETE")
                final_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                final_conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> WeftStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._acquire_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._release_connection(connection)

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._acquire_connection(query_only=True)
        try:
            yield connection
        finally:
            self._release_connection(connection, query_only=True)

    def _initialize(self) -> None:
        connection = self._acquire_connection()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            schema_row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if schema_row is not None:
                try:
                    existing_version = int(schema_row["value"])
                except (TypeError, ValueError) as exc:
                    raise WeftError("corrupt_state", "Persisted schema version is invalid") from exc
                if existing_version > SCHEMA_VERSION:
                    raise WeftError(
                        "unsupported_schema",
                        f"State database schema {existing_version} is newer than supported schema {SCHEMA_VERSION}",
                    )
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS teams (
                    team_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    settings_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agents (
                    team_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    model TEXT,
                    capabilities_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_seen REAL NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY (team_id, agent_id),
                    FOREIGN KEY (team_id) REFERENCES teams(team_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS agent_credentials (
                    team_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    rotated_at TEXT,
                    revoked_at TEXT,
                    rotation_count INTEGER NOT NULL DEFAULT 0 CHECK(rotation_count >= 0),
                    PRIMARY KEY (team_id, agent_id),
                    FOREIGN KEY (team_id, agent_id) REFERENCES agents(team_id, agent_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    normalized_objective TEXT NOT NULL,
                    objective_hash TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    preferred_model TEXT,
                    owner_id TEXT,
                    status TEXT NOT NULL,
                    claimed_by TEXT,
                    claimed_at REAL,
                    lease_until REAL,
                    fencing_token INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    idempotency_key TEXT,
                    FOREIGN KEY (team_id) REFERENCES teams(team_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(team_id, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_objective ON tasks(team_id, objective_hash);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_task_idempotency
                    ON tasks(team_id, created_by, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_model TEXT,
                    recipient_id TEXT,
                    kind TEXT NOT NULL,
                    task_id TEXT,
                    correlation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    trace_id TEXT,
                    signature TEXT,
                    priority INTEGER NOT NULL,
                    idempotency_key TEXT,
                    sent_at TEXT NOT NULL,
                    FOREIGN KEY (team_id) REFERENCES teams(team_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_messages_inbox
                    ON messages(team_id, recipient_id, sent_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_message_idempotency
                    ON messages(team_id, sender_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS message_reads (
                    team_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    read_at TEXT NOT NULL,
                    PRIMARY KEY (team_id, message_id, agent_id),
                    FOREIGN KEY (message_id) REFERENCES messages(message_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_id TEXT,
                    object_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_team ON events(team_id, created_at);
                CREATE TABLE IF NOT EXISTS pairings (
                    pairing_id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    display_code TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at TEXT,
                    consumed_by TEXT,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY (team_id) REFERENCES teams(team_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_pairings_team ON pairings(team_id, status, expires_at);
                CREATE TABLE IF NOT EXISTS pairing_credentials (
                    pairing_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (pairing_id) REFERENCES pairings(pairing_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    pairing_id TEXT NOT NULL UNIQUE,
                    agent_a TEXT NOT NULL,
                    agent_b TEXT NOT NULL,
                    state TEXT NOT NULL,
                    session_token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    last_activity REAL NOT NULL,
                    cursor_head INTEGER NOT NULL,
                    closed_at TEXT,
                    FOREIGN KEY (team_id) REFERENCES teams(team_id) ON DELETE CASCADE,
                    FOREIGN KEY (pairing_id) REFERENCES pairings(pairing_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_team ON sessions(team_id, state, last_activity);
                CREATE TABLE IF NOT EXISTS session_credentials (
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at TEXT,
                    PRIMARY KEY (session_id, agent_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_session_credentials_hash ON session_credentials(token_hash);
                CREATE TABLE IF NOT EXISTS session_cursors (
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    last_ack_seq INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, agent_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS session_events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    origin_agent TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    trace_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, seq),
                    UNIQUE(session_id, origin_agent, idempotency_key),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_session_events_replay ON session_events(session_id, seq);
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                COMMIT;
                """
            )
        finally:
            self._release_connection(connection)

    def _ensure_team(self, connection: sqlite3.Connection, team_id: str, name: str | None = None) -> None:
        _validate_id(team_id, "team_id")
        connection.execute(
            "INSERT OR IGNORE INTO teams(team_id, name, created_at, settings_json) VALUES (?, ?, ?, ?)",
            (team_id, name or team_id, _utc_now(), _json({"heartbeat_timeout_seconds": self.heartbeat_timeout})),
        )

    def _require_agent(self, connection: sqlite3.Connection, team_id: str, agent_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM agents WHERE team_id = ? AND agent_id = ?", (team_id, agent_id)
        ).fetchone()
        if row is None:
            raise WeftError("agent_not_registered", f"Agent '{agent_id}' is not registered in team '{team_id}'")
        return row

    @staticmethod
    def _token_hash(token: str) -> str:
        if not isinstance(token, str) or len(token) < 16 or len(token) > 512:
            raise WeftError("invalid_token", "Token must be a non-empty opaque capability")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _require_actor_credential(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        agent_id: str,
        actor_token: str | None,
    ) -> sqlite3.Row | None:
        """Validate an agent credential without exposing token material."""
        if actor_token is None:
            if self.require_actor_auth:
                raise WeftError("actor_auth_required", "A valid actor token is required")
            return None
        try:
            supplied_hash = self._token_hash(actor_token)
        except WeftError as exc:
            raise WeftError("actor_auth_invalid", "Actor token is invalid") from exc
        row = connection.execute(
            "SELECT * FROM agent_credentials WHERE team_id = ? AND agent_id = ?",
            (team_id, agent_id),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise WeftError("actor_auth_invalid", "Actor token is invalid")
        if not secrets.compare_digest(row["token_hash"], supplied_hash):
            raise WeftError("actor_auth_invalid", "Actor token is invalid")
        return row

    def _authorize_actor(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        agent_id: str,
        actor_token: str | None,
    ) -> sqlite3.Row:
        """Require proof when configured, then resolve the bound team member."""
        if not self.require_actor_auth and actor_token is None:
            return self._require_agent(connection, team_id, agent_id)
        if actor_token is None:
            raise WeftError("actor_auth_required", "A valid actor token is required")
        try:
            supplied_hash = self._token_hash(actor_token)
        except WeftError as exc:
            raise WeftError("actor_auth_invalid", "Actor token is invalid") from exc
        row = connection.execute(
            """
            SELECT agents.*, credentials.token_hash AS credential_token_hash,
                   credentials.revoked_at AS credential_revoked_at
            FROM agents
            JOIN agent_credentials AS credentials
              ON credentials.team_id = agents.team_id
             AND credentials.agent_id = agents.agent_id
            WHERE agents.team_id = ? AND agents.agent_id = ?
            """,
            (team_id, agent_id),
        ).fetchone()
        if row is None or row["credential_revoked_at"] is not None:
            raise WeftError("actor_auth_invalid", "Actor token is invalid")
        if not secrets.compare_digest(row["credential_token_hash"], supplied_hash):
            raise WeftError("actor_auth_invalid", "Actor token is invalid")
        return row

    def _authorize_optional_actor(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        agent_id: str | None,
        actor_token: str | None,
    ) -> sqlite3.Row | None:
        if agent_id is None:
            if self.require_actor_auth or actor_token is not None:
                raise WeftError("actor_auth_required", "agent_id and a valid actor token are required")
            return None
        _validate_id(agent_id, "agent_id")
        return self._authorize_actor(connection, team_id, agent_id, actor_token)

    def _issue_actor_credential(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        agent_id: str,
    ) -> str:
        raw_token = self._random_token("fst_actor")
        timestamp = _utc_now()
        connection.execute(
            """
            INSERT INTO agent_credentials(
                team_id, agent_id, token_hash, created_at, updated_at, rotated_at, revoked_at, rotation_count
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, 0)
            """,
            (team_id, agent_id, self._token_hash(raw_token), timestamp, timestamp),
        )
        self._insert_event(
            connection,
            team_id,
            "agent.credential_issued",
            agent_id,
            agent_id,
            {"credential_kind": "agent"},
        )
        return raw_token

    @staticmethod
    def _random_token(prefix: str) -> str:
        return f"{prefix}_{secrets.token_urlsafe(32)}"

    @staticmethod
    def _display_code() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(secrets.choice(alphabet) for _ in range(4))

    def _upsert_agent(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        agent_id: str,
        name: str | None,
        role: str,
        model: str | None,
        capabilities: Sequence[str] | None,
        metadata: dict[str, Any] | None,
    ) -> sqlite3.Row:
        _validate_id(agent_id, "agent_id")
        if not isinstance(role, str) or not role.strip():
            raise WeftError("invalid_argument", "role must be a non-empty string")
        capabilities = list(capabilities or [])
        if len(capabilities) > 64 or any(not isinstance(item, str) for item in capabilities):
            raise WeftError("invalid_argument", "capabilities must contain at most 64 strings")
        now = _epoch()
        connection.execute(
            """
            INSERT INTO agents(team_id, agent_id, name, role, model, capabilities_json, status, last_seen, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(team_id, agent_id) DO UPDATE SET
                name = excluded.name,
                role = excluded.role,
                model = excluded.model,
                capabilities_json = excluded.capabilities_json,
                status = 'active',
                last_seen = excluded.last_seen,
                metadata_json = excluded.metadata_json
            """,
            (team_id, agent_id, (name or agent_id)[:160], role[:160], model, _json(capabilities), now, _json(metadata or {})),
        )
        row = connection.execute("SELECT * FROM agents WHERE team_id = ? AND agent_id = ?", (team_id, agent_id)).fetchone()
        assert row is not None
        return row

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        event_type: str,
        actor_id: str | None,
        object_id: str | None,
        payload: Any,
    ) -> None:
        connection.execute(
            "INSERT INTO events(event_id, team_id, event_type, actor_id, object_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_new_id("evt"), team_id, event_type, actor_id, object_id, _json(payload), _utc_now()),
        )

    def _canonical_path(self, raw_path: str) -> str:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise WeftError("invalid_path", "Workspace paths must be non-empty strings")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve(strict=False)
            relative = resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise WeftError("path_outside_workspace", "Path must remain inside the configured workspace") from exc
        if str(relative) in ("", "."):
            raise WeftError("invalid_path", "The workspace root itself is not a file scope")
        return relative.as_posix()

    def _canonical_scope(self, raw_scope: Sequence[str] | str | None) -> list[str]:
        if raw_scope is None:
            return []
        values = [raw_scope] if isinstance(raw_scope, str) else list(raw_scope)
        if len(values) > 256:
            raise WeftError("invalid_argument", "A task may declare at most 256 scope paths")
        result: list[str] = []
        for value in values:
            path = self._canonical_path(value)
            if path.casefold() not in {item.casefold() for item in result}:
                result.append(path)
        return sorted(result, key=str.casefold)

    @staticmethod
    def _scope_conflicts(left: Sequence[str], right: Sequence[str]) -> bool:
        left_norm = [item.replace("\\", "/").casefold().strip("/") for item in left]
        right_norm = [item.replace("\\", "/").casefold().strip("/") for item in right]
        for first in left_norm:
            for second in right_norm:
                if first == second or first.startswith(second + "/") or second.startswith(first + "/"):
                    return True
        return False

    def _open_task_rows(self, connection: sqlite3.Connection, team_id: str) -> list[sqlite3.Row]:
        self._recover_expired_leases(connection, team_id)
        return list(
            connection.execute(
                "SELECT * FROM tasks WHERE team_id = ? AND status IN ('pending','assigned','in_progress','review','verified','blocked')",
                (team_id,),
            ).fetchall()
        )

    def _recover_expired_leases(self, connection: sqlite3.Connection, team_id: str) -> None:
        now = _epoch()
        rows = connection.execute(
            "SELECT * FROM tasks WHERE team_id = ? AND status IN ('assigned','in_progress','review','verified') AND lease_until IS NOT NULL AND lease_until < ?",
            (team_id, now),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE tasks SET status = 'pending', claimed_by = NULL, claimed_at = NULL, lease_until = NULL, version = version + 1, updated_at = ? WHERE task_id = ?",
                (_utc_now(), row["task_id"]),
            )
            self._insert_event(
                connection,
                team_id,
                "task.lease_expired",
                None,
                row["task_id"],
                {"previous_status": row["status"], "previous_owner": row["claimed_by"], "fencing_token": row["fencing_token"]},
            )

    def _route(self, connection: sqlite3.Connection, team_id: str, text: str, preferred_model: str | None = None) -> dict[str, Any] | None:
        agents = connection.execute(
            """
            SELECT agents.*, COALESCE(load.active_tasks, 0) AS active_tasks
            FROM agents
            LEFT JOIN (
                SELECT claimed_by, COUNT(*) AS active_tasks
                FROM tasks
                WHERE team_id = ? AND claimed_by IS NOT NULL
                  AND status IN ('assigned','in_progress','review')
                GROUP BY claimed_by
            ) AS load ON load.claimed_by = agents.agent_id
            WHERE agents.team_id = ? AND agents.status = 'active'
            ORDER BY agents.agent_id
            """,
            (team_id, team_id),
        ).fetchall()
        if not agents:
            return None
        requested = text.casefold()
        candidates: list[tuple[int, float, str, sqlite3.Row]] = []
        for agent in agents:
            capabilities = _parse_json(agent["capabilities_json"], [])
            haystack = " ".join([agent["role"], agent["name"], agent["model"] or "", *capabilities]).casefold()
            score = sum(1 for keywords in ROUTE_KEYWORDS.values() for keyword in keywords if keyword in requested and keyword in haystack)
            if preferred_model and agent["model"] == preferred_model:
                score += 10
            # Prefer a less-busy agent when capability scores tie.
            active_count = int(agent["active_tasks"])
            candidates.append((score, -active_count, agent["agent_id"], agent))
        candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
        score, negative_load, _, selected = candidates[0]
        return {
            "agent_id": selected["agent_id"],
            "agent_name": selected["name"],
            "model": selected["model"],
            "score": score,
            "active_tasks": -negative_load,
        }

    @staticmethod
    def _agent_dict(row: sqlite3.Row, stale_after: int) -> dict[str, Any]:
        age = max(0.0, _epoch() - float(row["last_seen"]))
        return {
            "agent_id": row["agent_id"],
            "name": row["name"],
            "role": row["role"],
            "model": row["model"],
            "capabilities": _parse_json(row["capabilities_json"], []),
            "status": row["status"],
            "last_seen": row["last_seen"],
            "age_seconds": round(age, 3),
            "stale": age > stale_after,
            "metadata": _parse_json(row["metadata_json"], {}),
        }

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "team_id": row["team_id"],
            "title": row["title"],
            "description": row["description"],
            "scope": _parse_json(row["scope_json"], []),
            "priority": row["priority"],
            "preferred_model": row["preferred_model"],
            "owner_id": row["owner_id"],
            "status": row["status"],
            "claimed_by": row["claimed_by"],
            "claimed_at": row["claimed_at"],
            "lease_until": row["lease_until"],
            "fencing_token": row["fencing_token"],
            "version": row["version"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "progress": row["progress"],
            "metadata": _parse_json(row["metadata_json"], {}),
        }

    def register_agent(
        self,
        team_id: str,
        agent_id: str,
        name: str | None = None,
        role: str = "generalist",
        model: str | None = None,
        capabilities: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        with self._transaction() as connection:
            self._ensure_team(connection, team_id)
            existing = connection.execute(
                "SELECT * FROM agents WHERE team_id = ? AND agent_id = ?",
                (team_id, agent_id),
            ).fetchone()
            if existing is not None:
                self._authorize_actor(connection, team_id, agent_id, actor_token)
            row = self._upsert_agent(connection, team_id, agent_id, name, role, model, capabilities, metadata)
            issued_token = None if existing is not None else self._issue_actor_credential(connection, team_id, agent_id)
            self._insert_event(connection, team_id, "agent.registered", agent_id, agent_id, {"model": model, "role": role})
            result = self._agent_dict(row, self.heartbeat_timeout)
            if issued_token is not None:
                result["actor_token"] = issued_token
            return result

    def rotate_agent_credential(
        self,
        team_id: str,
        agent_id: str,
        current_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        with self._transaction() as connection:
            credential = connection.execute(
                "SELECT * FROM agent_credentials WHERE team_id = ? AND agent_id = ?",
                (team_id, agent_id),
            ).fetchone()
            if self.require_actor_auth:
                self._require_actor_credential(connection, team_id, agent_id, current_token)
            elif current_token is not None:
                self._require_actor_credential(connection, team_id, agent_id, current_token)
            self._require_agent(connection, team_id, agent_id)

            replacement = self._random_token("fst_actor")
            replacement_hash = self._token_hash(replacement)
            timestamp = _utc_now()
            bootstrapped = credential is None
            if credential is None:
                connection.execute(
                    """
                    INSERT INTO agent_credentials(
                        team_id, agent_id, token_hash, created_at, updated_at, rotated_at, revoked_at, rotation_count
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, 0)
                    """,
                    (team_id, agent_id, replacement_hash, timestamp, timestamp),
                )
            else:
                connection.execute(
                    """
                    UPDATE agent_credentials
                    SET token_hash = ?, updated_at = ?, rotated_at = ?, revoked_at = NULL,
                        rotation_count = rotation_count + 1
                    WHERE team_id = ? AND agent_id = ?
                    """,
                    (replacement_hash, timestamp, timestamp, team_id, agent_id),
                )
            updated = connection.execute(
                "SELECT rotation_count FROM agent_credentials WHERE team_id = ? AND agent_id = ?",
                (team_id, agent_id),
            ).fetchone()
            assert updated is not None
            self._insert_event(
                connection,
                team_id,
                "agent.credential_bootstrapped" if bootstrapped else "agent.credential_rotated",
                agent_id,
                agent_id,
                {"credential_kind": "agent", "rotation_count": updated["rotation_count"]},
            )
            return {
                "team_id": team_id,
                "agent_id": agent_id,
                "actor_token": replacement,
                "bootstrapped": bootstrapped,
                "rotation_count": updated["rotation_count"],
            }

    def route_task(
        self,
        team_id: str,
        title: str,
        description: str,
        preferred_model: str | None = None,
        agent_id: str | None = None,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        normalized = _normalize_objective(title, description)
        with self._read() as connection:
            self._authorize_optional_actor(connection, team_id, agent_id, actor_token)
            selection = self._route(connection, team_id, normalized, preferred_model)
        return {"team_id": team_id, "objective": normalized, "preferred_model": preferred_model, "selection": selection}

    def create_task(
        self,
        team_id: str,
        created_by: str,
        title: str,
        description: str = "",
        scope: Sequence[str] | str | None = None,
        priority: int = 2,
        preferred_agent: str | None = None,
        preferred_model: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(created_by, "created_by")
        if not isinstance(title, str) or not title.strip() or len(title) > 240:
            raise WeftError("invalid_argument", "title must be 1-240 characters")
        if not isinstance(description, str) or len(description) > MAX_PAYLOAD_BYTES:
            raise WeftError("invalid_argument", "description is too long")
        if preferred_agent is not None:
            _validate_id(preferred_agent, "preferred_agent")
        normalized = _normalize_objective(title, description)
        objective_hash = _hash_objective(normalized)
        canonical_scope = self._canonical_scope(scope)
        priority = _safe_int(priority, 2, 0, 3)
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or len(idempotency_key) > 160):
            raise WeftError("invalid_argument", "idempotency_key must be at most 160 characters")
        with self._transaction() as connection:
            self._authorize_actor(connection, team_id, created_by, actor_token)
            if idempotency_key:
                prior = connection.execute(
                    "SELECT * FROM tasks WHERE team_id = ? AND created_by = ? AND idempotency_key = ?",
                    (team_id, created_by, idempotency_key),
                ).fetchone()
                if prior is not None:
                    return {"created": False, "idempotent": True, "task": self._task_dict(prior), "routing": None}
            open_tasks = self._open_task_rows(connection, team_id)
            duplicate: dict[str, Any] | None = None
            for row in open_tasks:
                ratio = difflib.SequenceMatcher(None, normalized[:MAX_COMPARE_CHARS], row["normalized_objective"][:MAX_COMPARE_CHARS]).ratio()
                same_scope = self._scope_conflicts(canonical_scope, _parse_json(row["scope_json"], [])) if canonical_scope else False
                if ratio >= 0.55 and (same_scope or not canonical_scope or not _parse_json(row["scope_json"], [])):
                    duplicate = {"task_id": row["task_id"], "title": row["title"], "status": row["status"], "owner_id": row["owner_id"], "similarity": round(ratio, 3)}
                    break
            if duplicate:
                return {"created": False, "duplicate": duplicate, "task": None, "routing": None}
            routing = self._route(connection, team_id, normalized, preferred_model)
            owner_id = preferred_agent or (routing["agent_id"] if routing else None)
            if preferred_agent:
                agent = connection.execute(
                    "SELECT agent_id FROM agents WHERE team_id = ? AND agent_id = ? AND status = 'active'",
                    (team_id, preferred_agent),
                ).fetchone()
                if agent is None:
                    raise WeftError("agent_unavailable", f"Agent '{preferred_agent}' is not active in team '{team_id}'")
            task_id = _new_id("task")
            timestamp = _utc_now()
            row = connection.execute(
                """
                INSERT INTO tasks(task_id, team_id, title, description, normalized_objective, objective_hash, scope_json,
                                  priority, preferred_model, owner_id, status, claimed_by, claimed_at, lease_until,
                                  fencing_token, version, created_by, created_at, updated_at, progress, metadata_json, idempotency_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, 1, ?, ?, ?, 0, ?, ?)
                RETURNING *
                """,
                (task_id, team_id, title.strip(), description, normalized, objective_hash, _json(canonical_scope), priority, preferred_model,
                 owner_id, "pending", created_by, timestamp, timestamp, _json(metadata or {}), idempotency_key),
            ).fetchone()
            assert row is not None
            self._insert_event(connection, team_id, "task.created", created_by, task_id, {"owner_id": owner_id, "scope": canonical_scope, "priority": priority})
            return {"created": True, "task": self._task_dict(row), "routing": routing}

    def claim_task(
        self,
        team_id: str,
        agent_id: str,
        task_id: str,
        lease_seconds: int | None = None,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        _validate_id(task_id, "task_id")
        lease_seconds = _safe_int(lease_seconds, self.heartbeat_timeout, 30, 86_400)
        with self._transaction() as connection:
            self._authorize_actor(connection, team_id, agent_id, actor_token)
            self._recover_expired_leases(connection, team_id)
            row = connection.execute("SELECT * FROM tasks WHERE team_id = ? AND task_id = ?", (team_id, task_id)).fetchone()
            if row is None:
                raise WeftError("task_not_found", f"Task '{task_id}' was not found")
            if row["status"] in {"done", "cancelled", "verified"}:
                raise WeftError("task_not_claimable", f"Task '{task_id}' is already {row['status']}")
            if row["claimed_by"] and row["claimed_by"] != agent_id and row["status"] in {"assigned", "in_progress", "review"}:
                raise WeftError("task_claim_conflict", "Task is already leased to another agent", {"claimed_by": row["claimed_by"], "fencing_token": row["fencing_token"]})
            scope = _parse_json(row["scope_json"], [])
            active_rows = connection.execute(
                "SELECT * FROM tasks WHERE team_id = ? AND task_id != ? AND status IN ('assigned','in_progress','review') AND claimed_by IS NOT NULL",
                (team_id, task_id),
            ).fetchall()
            for other in active_rows:
                other_scope = _parse_json(other["scope_json"], [])
                if scope and other_scope and self._scope_conflicts(scope, other_scope):
                    raise WeftError("scope_lock_conflict", "Another active task owns an overlapping file scope", {"task_id": other["task_id"], "claimed_by": other["claimed_by"], "scope": other_scope})
            token = secrets.randbits(51)  # <= 2^51-1, inside JS safe-integer range (2^53-1)
            lease_until = _epoch() + lease_seconds
            updated = connection.execute(
                "UPDATE tasks SET status = 'in_progress', claimed_by = ?, claimed_at = ?, lease_until = ?, fencing_token = ?, version = version + 1, updated_at = ? WHERE task_id = ? RETURNING *",
                (agent_id, _epoch(), lease_until, token, _utc_now(), task_id),
            ).fetchone()
            assert updated is not None
            self._insert_event(connection, team_id, "task.claimed", agent_id, task_id, {"lease_until": lease_until, "fencing_token": token, "scope": scope})
            return self._task_dict(updated)

    def update_task(
        self,
        team_id: str,
        agent_id: str,
        task_id: str,
        status: str | None = None,
        progress: int | None = None,
        note: str | None = None,
        fencing_token: int | None = None,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        _validate_id(task_id, "task_id")
        if status is not None and status not in TASK_STATUSES:
            raise WeftError("invalid_status", f"Unknown task status '{status}'")
        if progress is not None:
            progress = _safe_int(progress, 0, 0, 100)
        if note is not None and (not isinstance(note, str) or len(note) > 8_000):
            raise WeftError("invalid_argument", "note must be at most 8000 characters")
        with self._transaction() as connection:
            self._authorize_actor(connection, team_id, agent_id, actor_token)
            self._recover_expired_leases(connection, team_id)
            row = connection.execute("SELECT * FROM tasks WHERE team_id = ? AND task_id = ?", (team_id, task_id)).fetchone()
            if row is None:
                raise WeftError("task_not_found", f"Task '{task_id}' was not found")
            if row["claimed_by"] != agent_id:
                raise WeftError("not_task_owner", "Only the current lease owner may update this task")
            if fencing_token is None or fencing_token != row["fencing_token"]:
                raise WeftError("stale_fencing_token", "The fencing token does not match the current lease")
            if row["lease_until"] is not None and row["lease_until"] < _epoch():
                raise WeftError("lease_expired", "The task lease has expired; reclaim it before updating")
            if status == "done":
                raise WeftError("quality_gate_required", "Use complete_task after a passed quality gate")
            next_status = status or row["status"]
            if status == "verified":
                raise WeftError("quality_gate_required", "Use verify_task to reach verified")
            if row["status"] == "verified" and next_status != "verified":
                raise WeftError("invalid_transition", "A verified task can only be completed")
            fields: list[str] = ["status = ?", "progress = ?", "version = version + 1", "updated_at = ?"]
            params: list[Any] = [next_status, progress if progress is not None else row["progress"], _utc_now()]
            if next_status in {"blocked", "cancelled"}:
                fields.extend(["lease_until = NULL", "claimed_by = NULL"])
            updated = connection.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ? RETURNING *",
                [*params, task_id],
            ).fetchone()
            assert updated is not None
            self._insert_event(connection, team_id, "task.updated", agent_id, task_id, {"status": next_status, "progress": progress, "note": note})
            return self._task_dict(updated)

    def send_message(
        self,
        team_id: str,
        sender_id: str,
        kind: str,
        payload: dict[str, Any] | list[Any] | str,
        recipient_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
        priority: int = 2,
        capabilities: Sequence[str] | None = None,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(sender_id, "sender_id")
        if recipient_id is not None:
            _validate_id(recipient_id, "recipient_id")
        if task_id is not None:
            _validate_id(task_id, "task_id")
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_.:-]{1,63}", kind):
            raise WeftError("invalid_message_type", "kind must be a lowercase namespaced event type")
        priority = _safe_int(priority, 2, 0, 3)
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or len(idempotency_key) > 160):
            raise WeftError("invalid_argument", "idempotency_key must be at most 160 characters")
        encoded_payload = _json(payload)
        encoded_capabilities = _json(list(capabilities or []))
        with self._transaction() as connection:
            sender = self._authorize_actor(connection, team_id, sender_id, actor_token)
            if recipient_id is not None:
                self._require_agent(connection, team_id, recipient_id)
            if task_id is not None:
                task = connection.execute("SELECT task_id FROM tasks WHERE team_id = ? AND task_id = ?", (team_id, task_id)).fetchone()
                if task is None:
                    raise WeftError("task_not_found", f"Task '{task_id}' was not found")
            message_id = _new_id("msg")
            sent_at = _utc_now()
            correlation_id = correlation_id or task_id or message_id
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO messages(message_id, team_id, sender_id, sender_model, recipient_id, kind, task_id, correlation_id,
                                                payload_json, capabilities_json, trace_id, signature, priority, idempotency_key, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (message_id, team_id, sender_id, sender["model"], recipient_id, kind, task_id, correlation_id, encoded_payload,
                 encoded_capabilities, trace_id, priority, idempotency_key, sent_at),
            ).rowcount
            if inserted == 0:
                prior = None
                if idempotency_key:
                    prior = connection.execute(
                        "SELECT * FROM messages WHERE team_id = ? AND sender_id = ? AND idempotency_key = ?",
                        (team_id, sender_id, idempotency_key),
                    ).fetchone()
                if prior is None:
                    raise WeftError("state_conflict", "Message insert conflicted with persisted state")
                return {"sent": False, "idempotent": True, "message": self._message_dict(connection, prior, sender_id)}
            self._insert_event(connection, team_id, "message.sent", sender_id, message_id, {"kind": kind, "recipient_id": recipient_id, "task_id": task_id, "correlation_id": correlation_id})
            return {
                "sent": True,
                "message": {
                    "protocol": WEFT_PROTOCOL,
                    "version": WEFT_VERSION,
                    "message_id": message_id,
                    "type": kind,
                    "task_id": task_id,
                    "sender": {"agent_id": sender_id, "model": sender["model"]},
                    "recipient": {"scope": "broadcast"} if recipient_id is None else {"agent_id": recipient_id},
                    "timestamp": sent_at,
                    "correlation_id": correlation_id,
                    "payload": _parse_json(encoded_payload, None),
                    "capabilities": _parse_json(encoded_capabilities, []),
                    "trace_id": trace_id,
                    "signature": None,
                    "priority": priority,
                    "read": False,
                },
            }

    def _message_dict(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        viewer_id: str | None = None,
        read: bool | None = None,
    ) -> dict[str, Any]:
        recipient = {"scope": "broadcast"} if row["recipient_id"] is None else {"agent_id": row["recipient_id"]}
        read_known = read is not None
        if read is None:
            read = False
        if viewer_id and not read_known:
            read = connection.execute(
                "SELECT 1 FROM message_reads WHERE team_id = ? AND message_id = ? AND agent_id = ?",
                (row["team_id"], row["message_id"], viewer_id),
            ).fetchone() is not None
        return {
            "protocol": WEFT_PROTOCOL,
            "version": WEFT_VERSION,
            "message_id": row["message_id"],
            "type": row["kind"],
            "task_id": row["task_id"],
            "sender": {"agent_id": row["sender_id"], "model": row["sender_model"]},
            "recipient": recipient,
            "timestamp": row["sent_at"],
            "correlation_id": row["correlation_id"],
            "payload": _parse_json(row["payload_json"], None),
            "capabilities": _parse_json(row["capabilities_json"], []),
            "trace_id": row["trace_id"],
            "signature": row["signature"],
            "priority": row["priority"],
            "read": read,
        }

    def read_inbox(
        self,
        team_id: str,
        agent_id: str,
        limit: int = 50,
        unread_only: bool = True,
        acknowledge: bool = True,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        limit = _safe_int(limit, 50, 1, 200)
        with self._transaction() as connection:
            self._authorize_actor(connection, team_id, agent_id, actor_token)
            rows = connection.execute(
                """
                SELECT m.*, r.message_id AS viewer_read_message_id FROM messages m
                LEFT JOIN message_reads r ON r.team_id = m.team_id AND r.message_id = m.message_id AND r.agent_id = ?
                WHERE m.team_id = ? AND (m.recipient_id = ? OR m.recipient_id IS NULL)
                  AND (? = 0 OR r.message_id IS NULL)
                ORDER BY m.priority ASC, m.sent_at ASC LIMIT ?
                """,
                (agent_id, team_id, agent_id, 1 if unread_only else 0, limit),
            ).fetchall()
            messages = [
                self._message_dict(
                    connection,
                    row,
                    agent_id,
                    read=row["viewer_read_message_id"] is not None,
                )
                for row in rows
            ]
            if acknowledge:
                for row in rows:
                    connection.execute(
                        "INSERT OR IGNORE INTO message_reads(team_id, message_id, agent_id, read_at) VALUES (?, ?, ?, ?)",
                        (team_id, row["message_id"], agent_id, _utc_now()),
                    )
                if rows:
                    self._insert_event(connection, team_id, "message.acknowledged", agent_id, None, {"message_ids": [row["message_id"] for row in rows]})
            return {"team_id": team_id, "agent_id": agent_id, "messages": messages, "count": len(messages)}

    def acknowledge_message(
        self,
        team_id: str,
        agent_id: str,
        message_id: str,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        _validate_id(message_id, "message_id")
        with self._transaction() as connection:
            self._authorize_actor(connection, team_id, agent_id, actor_token)
            message = connection.execute(
                "SELECT recipient_id FROM messages WHERE team_id = ? AND message_id = ?",
                (team_id, message_id),
            ).fetchone()
            if message is None:
                raise WeftError("message_not_found", f"Message '{message_id}' was not found")
            if message["recipient_id"] is not None and message["recipient_id"] != agent_id:
                raise WeftError("message_forbidden", "Message is addressed to a different agent")
            inserted = connection.execute(
                "INSERT OR IGNORE INTO message_reads(team_id, message_id, agent_id, read_at) VALUES (?, ?, ?, ?)",
                (team_id, message_id, agent_id, _utc_now()),
            ).rowcount
            if inserted:
                self._insert_event(
                    connection,
                    team_id,
                    "message.acknowledged",
                    agent_id,
                    message_id,
                    {"message_ids": [message_id]},
                )
            return {
                "team_id": team_id,
                "agent_id": agent_id,
                "message_id": message_id,
                "acknowledged": True,
                "idempotent": inserted == 0,
            }

    def heartbeat(
        self,
        team_id: str,
        agent_id: str,
        task_ids: Sequence[str] | None = None,
        fencing_tokens: dict[str, int] | None = None,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        task_ids = list(task_ids or [])
        fencing_tokens = fencing_tokens or {}
        with self._transaction() as connection:
            self._authorize_actor(connection, team_id, agent_id, actor_token)
            now = _epoch()
            connection.execute("UPDATE agents SET last_seen = ?, status = 'active' WHERE team_id = ? AND agent_id = ?", (now, team_id, agent_id))
            self._recover_expired_leases(connection, team_id)
            rows = connection.execute(
                "SELECT * FROM tasks WHERE team_id = ? AND claimed_by = ? AND status IN ('in_progress','review','verified')", (team_id, agent_id)
            ).fetchall()
            renewed: list[str] = []
            rejected: list[dict[str, Any]] = []
            for row in rows:
                if task_ids and row["task_id"] not in task_ids:
                    continue
                supplied_token = fencing_tokens.get(row["task_id"])
                if supplied_token is not None and supplied_token != row["fencing_token"]:
                    rejected.append({"task_id": row["task_id"], "reason": "stale_fencing_token"})
                    continue
                lease_until = now + self.heartbeat_timeout
                connection.execute("UPDATE tasks SET lease_until = ?, updated_at = ? WHERE task_id = ?", (lease_until, _utc_now(), row["task_id"]))
                renewed.append(row["task_id"])
            self._insert_event(connection, team_id, "agent.heartbeat", agent_id, agent_id, {"renewed_tasks": renewed, "rejected_tasks": rejected})
            return {"team_id": team_id, "agent_id": agent_id, "last_seen": now, "renewed_tasks": renewed, "rejected_tasks": rejected}

    def verify_task(
        self,
        team_id: str,
        agent_id: str,
        task_id: str,
        fencing_token: int,
        files: Sequence[str] | None,
        checks: Sequence[dict[str, Any]],
        reviewer_id: str | None = None,
        require_review: bool = False,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        _validate_id(task_id, "task_id")
        if reviewer_id is not None:
            _validate_id(reviewer_id, "reviewer_id")
        if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)) or not checks:
            raise WeftError("quality_gate_failed", "At least one machine-readable check is required")
        if len(checks) > 64:
            raise WeftError("invalid_argument", "At most 64 checks may be submitted")
        with self._transaction() as connection:
            self._authorize_actor(connection, team_id, agent_id, actor_token)
            if reviewer_id:
                self._require_agent(connection, team_id, reviewer_id)
            self._recover_expired_leases(connection, team_id)
            task = connection.execute("SELECT * FROM tasks WHERE team_id = ? AND task_id = ?", (team_id, task_id)).fetchone()
            if task is None:
                raise WeftError("task_not_found", f"Task '{task_id}' was not found")
            if task["claimed_by"] != agent_id:
                raise WeftError("not_task_owner", "Only the current lease owner may submit evidence")
            if fencing_token != task["fencing_token"]:
                raise WeftError("stale_fencing_token", "The fencing token does not match the current lease")
            if task["lease_until"] is not None and task["lease_until"] < _epoch():
                raise WeftError("lease_expired", "The task lease has expired; reclaim it before verification")
            declared_scope = _parse_json(task["scope_json"], [])
            file_records: list[dict[str, Any]] = []
            scope_violations: list[str] = []
            secret_findings: list[str] = []
            for raw_path in list(files or []):
                relative = self._canonical_path(raw_path)
                if declared_scope and not any(relative.casefold() == scope.casefold() or relative.casefold().startswith(scope.casefold().rstrip("/") + "/") for scope in declared_scope):
                    scope_violations.append(relative)
                path = self.workspace / relative
                if not path.exists() or not path.is_file():
                    file_records.append({"path": relative, "exists": False})
                    continue
                size = path.stat().st_size
                if size > MAX_FILE_BYTES:
                    file_records.append({"path": relative, "exists": True, "bytes": size, "too_large": True})
                    continue
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                text = raw.decode("utf-8", errors="ignore")
                for pattern in SECRET_PATTERNS:
                    if pattern.search(text):
                        secret_findings.append(relative)
                        break
                file_records.append({"path": relative, "exists": True, "bytes": size, "sha256": digest})
            normalized_checks: list[dict[str, Any]] = []
            check_failures: list[str] = []
            for check in checks:
                if not isinstance(check, dict) or not isinstance(check.get("name"), str):
                    raise WeftError("quality_gate_failed", "Each check must include a name and status")
                status = check.get("status")
                if status not in {"passed", "failed", "unknown"}:
                    raise WeftError("quality_gate_failed", "Check status must be passed, failed, or unknown")
                if status != "passed":
                    check_failures.append(check["name"])
                normalized_checks.append({
                    "name": check["name"][:160],
                    "status": status,
                    "command": str(check.get("command", ""))[:400],
                    "evidence": str(check.get("evidence", ""))[:2_000],
                })
            review_ok = not require_review or (reviewer_id is not None and reviewer_id != agent_id)
            missing_files = [record["path"] for record in file_records if not record.get("exists")]
            oversized_files = [record["path"] for record in file_records if record.get("too_large")]
            passed = not scope_violations and not secret_findings and not check_failures and review_ok and not missing_files and not oversized_files
            payload = {
                "files": file_records,
                "checks": normalized_checks,
                "scope": declared_scope,
                "scope_violations": scope_violations,
                "secret_scan": {"status": "passed" if not secret_findings else "failed", "files": secret_findings},
                "review": {"required": require_review, "reviewer_id": reviewer_id, "passed": review_ok},
                "failures": {"checks": check_failures, "missing_files": missing_files, "oversized_files": oversized_files},
            }
            evidence_id = _new_id("evidence")
            evidence_status = "passed" if passed else "failed"
            connection.execute(
                "INSERT INTO evidence(evidence_id, team_id, task_id, agent_id, status, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (evidence_id, team_id, task_id, agent_id, evidence_status, _json(payload), _utc_now()),
            )
            next_status = "verified" if passed else "review"
            connection.execute("UPDATE tasks SET status = ?, updated_at = ?, version = version + 1 WHERE task_id = ?", (next_status, _utc_now(), task_id))
            self._insert_event(connection, team_id, "quality.evaluated", agent_id, task_id, {"evidence_id": evidence_id, "status": evidence_status, "failures": payload["failures"]})
            return {"passed": passed, "evidence_id": evidence_id, "status": evidence_status, "task_status": next_status, "details": payload}

    def complete_task(
        self,
        team_id: str,
        agent_id: str,
        task_id: str,
        fencing_token: int,
        summary: str = "",
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        _validate_id(task_id, "task_id")
        if not isinstance(summary, str) or len(summary) > 8_000:
            raise WeftError("invalid_argument", "summary must be at most 8000 characters")
        with self._transaction() as connection:
            self._authorize_actor(connection, team_id, agent_id, actor_token)
            task = connection.execute("SELECT * FROM tasks WHERE team_id = ? AND task_id = ?", (team_id, task_id)).fetchone()
            if task is None:
                raise WeftError("task_not_found", f"Task '{task_id}' was not found")
            if task["claimed_by"] != agent_id or fencing_token != task["fencing_token"]:
                raise WeftError("stale_fencing_token", "Only the current lease owner may complete this task")
            if task["status"] != "verified":
                raise WeftError("quality_gate_required", "Task must have a passed quality gate before completion", {"status": task["status"]})
            if task["lease_until"] is not None and task["lease_until"] < _epoch():
                raise WeftError("lease_expired", "The task lease has expired; renew it before completion")
            metadata = _parse_json(task["metadata_json"], {})
            if summary:
                metadata["completion_summary"] = summary
            completed = connection.execute(
                "UPDATE tasks SET status = 'done', progress = 100, lease_until = NULL, updated_at = ?, version = version + 1, metadata_json = ? WHERE task_id = ? RETURNING *",
                (_utc_now(), _json(metadata), task_id),
            ).fetchone()
            assert completed is not None
            self._insert_event(connection, team_id, "task.completed", agent_id, task_id, {"summary": summary})
            return self._task_dict(completed)

    def create_pairing(
        self,
        initiator_id: str,
        team_id: str | None = None,
        ttl_seconds: int = 900,
        capabilities_offered: Sequence[str] | None = None,
        policy: dict[str, Any] | None = None,
        invitee_hint: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        team_id = team_id or _new_id("team")
        _validate_id(team_id, "team_id")
        _validate_id(initiator_id, "initiator_id")
        ttl_seconds = _safe_int(ttl_seconds, 900, 60, 3_600)
        capabilities = list(capabilities_offered or [])
        if len(capabilities) > 64 or any(not isinstance(item, str) for item in capabilities):
            raise WeftError("invalid_argument", "capabilities_offered must contain at most 64 strings")
        if invitee_hint is not None and len(invitee_hint) > 160:
            raise WeftError("invalid_argument", "invitee_hint must be at most 160 characters")
        raw_token = self._random_token("fst_pair")
        token_hash = self._token_hash(raw_token)
        raw_initiator_session_token = self._random_token("fst_session")
        initiator_session_token_hash = self._token_hash(raw_initiator_session_token)
        pairing_id = _new_id("pair")
        display_code = self._display_code()
        now = _epoch()
        expires_at = now + ttl_seconds
        with self._transaction() as connection:
            self._ensure_team(connection, team_id)
            self._authorize_actor(connection, team_id, initiator_id, actor_token)
            connection.execute(
                """
                INSERT INTO pairings(pairing_id, team_id, created_by, token_hash, display_code, capabilities_json,
                                      policy_json, status, created_at, expires_at, consumed_at, consumed_by, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'issued', ?, ?, NULL, NULL, ?)
                """,
                (pairing_id, team_id, initiator_id, token_hash, display_code, _json(capabilities), _json(policy or {"requires_consent": True}), _utc_now(), expires_at, _json({"invitee_hint": invitee_hint, **(metadata or {})})),
            )
            connection.execute(
                "INSERT INTO pairing_credentials(pairing_id, agent_id, token_hash, created_at) VALUES (?, ?, ?, ?)",
                (pairing_id, initiator_id, initiator_session_token_hash, _utc_now()),
            )
            self._insert_event(connection, team_id, "pairing.issued", initiator_id, pairing_id, {"expires_at": expires_at, "capabilities": capabilities})
        join_base = self.public_base_url if self.public_base_url.endswith("/v1") else f"{self.public_base_url}/v1"
        join_url = f"{join_base}/join/{pairing_id}#token={quote(raw_token, safe='')}"
        bootstrap_prompt = (
            "Join the Weft team using this one-time link:\n"
            f"{join_url}\n"
            "Review the offered capabilities, extract the token from the URL fragment, then call "
            "pairing_preview followed by join_pairing with consent=true. "
            "Never copy the token into logs or public transcripts outside this intended handoff."
        )
        return {
            "pairing_id": pairing_id,
            "team_id": team_id,
            "display_code": display_code,
            "join_token": raw_token,
            "join_url": join_url,
            "initiator_session_token": raw_initiator_session_token,
            "expires_at": expires_at,
            "capabilities_offered": capabilities,
            "policy": policy or {"requires_consent": True},
            "bootstrap_prompt": bootstrap_prompt,
        }

    @staticmethod
    def _pairing_preview_dict(row: sqlite3.Row, public: bool = False) -> dict[str, Any]:
        status = row["status"]
        if status == "issued" and row["expires_at"] < _epoch():
            status = "expired"
        return {
            "pairing_id": row["pairing_id"],
            "team_id": None if public else row["team_id"],
            "status": status,
            "created_by": row["created_by"],
            "expires_at": row["expires_at"],
            "capabilities_offered": _parse_json(row["capabilities_json"], []),
            "policy": _parse_json(row["policy_json"], {}),
            "invitee_hint": _parse_json(row["metadata_json"], {}).get("invitee_hint"),
        }

    def pairing_preview(self, token: str) -> dict[str, Any]:
        token_hash = self._token_hash(token)
        with self._read() as connection:
            row = connection.execute("SELECT * FROM pairings WHERE token_hash = ?", (token_hash,)).fetchone()
            if row is None:
                raise WeftError("pairing_not_found", "Pairing link is invalid or has been revoked")
            return self._pairing_preview_dict(row)

    def pairing_preview_by_id(self, pairing_id: str, public: bool = False) -> dict[str, Any]:
        _validate_id(pairing_id, "pairing_id")
        with self._read() as connection:
            row = connection.execute("SELECT * FROM pairings WHERE pairing_id = ?", (pairing_id,)).fetchone()
            if row is None:
                raise WeftError("pairing_not_found", "Pairing link is invalid or has been revoked")
            return self._pairing_preview_dict(row, public=public)

    def join_pairing(
        self,
        token: str,
        agent_id: str,
        name: str | None = None,
        role: str = "generalist",
        model: str | None = None,
        capabilities: Sequence[str] | None = None,
        consent: bool = False,
        session_ttl_seconds: int = 86_400,
        metadata: dict[str, Any] | None = None,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(consent, bool):
            raise WeftError("invalid_consent", "consent must be the JSON boolean true")
        if consent is not True:
            raise WeftError("consent_required", "Joining a team requires explicit consent=true")
        token_hash = self._token_hash(token)
        _validate_id(agent_id, "agent_id")
        session_ttl_seconds = _safe_int(session_ttl_seconds, 86_400, 300, 604_800)
        with self._transaction() as connection:
            pairing = connection.execute("SELECT * FROM pairings WHERE token_hash = ?", (token_hash,)).fetchone()
            if pairing is None:
                raise WeftError("pairing_not_found", "Pairing link is invalid or has been revoked")
            now = _epoch()
            if pairing["status"] != "issued":
                raise WeftError("pairing_unavailable", f"Pairing is {pairing['status']}")
            if pairing["expires_at"] < now:
                connection.execute("UPDATE pairings SET status = 'expired' WHERE pairing_id = ?", (pairing["pairing_id"],))
                raise WeftError("pairing_expired", "Pairing link has expired; request a new link")
            if pairing["created_by"] == agent_id:
                raise WeftError("pairing_self_join", "The initiator cannot join its own pairing link")
            existing_agent = connection.execute(
                "SELECT * FROM agents WHERE team_id = ? AND agent_id = ?",
                (pairing["team_id"], agent_id),
            ).fetchone()
            if existing_agent is not None:
                self._authorize_actor(connection, pairing["team_id"], agent_id, actor_token)
            initiator_credential = connection.execute("SELECT * FROM pairing_credentials WHERE pairing_id = ?", (pairing["pairing_id"],)).fetchone()
            if initiator_credential is None:
                raise WeftError("pairing_corrupt", "Pairing is missing its initiator credential")
            changed = connection.execute(
                "UPDATE pairings SET status = 'consumed', consumed_at = ?, consumed_by = ? WHERE pairing_id = ? AND status = 'issued' AND expires_at >= ?",
                (_utc_now(), agent_id, pairing["pairing_id"], now),
            ).rowcount
            if changed != 1:
                raise WeftError("pairing_race", "Pairing link was consumed by another join attempt")
            self._ensure_team(connection, pairing["team_id"])
            self._upsert_agent(connection, pairing["team_id"], agent_id, name, role, model, capabilities, metadata)
            issued_actor_token = None
            if existing_agent is None:
                issued_actor_token = self._issue_actor_credential(connection, pairing["team_id"], agent_id)
            session_id = _new_id("session")
            raw_session_token = self._random_token("fst_session")
            session_token_hash = self._token_hash(raw_session_token)
            session_expires = now + session_ttl_seconds
            connection.execute(
                """
                INSERT INTO sessions(session_id, team_id, pairing_id, agent_a, agent_b, state, session_token_hash,
                                      created_at, expires_at, last_activity, cursor_head, closed_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, 0, NULL)
                """,
                (session_id, pairing["team_id"], pairing["pairing_id"], pairing["created_by"], agent_id, session_token_hash, _utc_now(), session_expires, now),
            )
            connection.execute(
                "INSERT INTO session_credentials(session_id, agent_id, token_hash, created_at, expires_at, revoked_at) VALUES (?, ?, ?, ?, ?, NULL), (?, ?, ?, ?, ?, NULL)",
                (session_id, pairing["created_by"], initiator_credential["token_hash"], _utc_now(), session_expires, session_id, agent_id, session_token_hash, _utc_now(), session_expires),
            )
            connection.execute("DELETE FROM pairing_credentials WHERE pairing_id = ?", (pairing["pairing_id"],))
            connection.execute(
                "INSERT INTO session_cursors(session_id, agent_id, last_ack_seq, updated_at) VALUES (?, ?, 0, ?), (?, ?, 0, ?)",
                (session_id, pairing["created_by"], _utc_now(), session_id, agent_id, _utc_now()),
            )
            self._insert_event(connection, pairing["team_id"], "pairing.joined", agent_id, pairing["pairing_id"], {"session_id": session_id, "initiator_id": pairing["created_by"], "consent": True})
            self._insert_event(connection, pairing["team_id"], "session.active", agent_id, session_id, {"agent_a": pairing["created_by"], "agent_b": agent_id})
            members = [pairing["created_by"], agent_id]
            result = {
                "session_id": session_id,
                "session_token": raw_session_token,
                "team_id": pairing["team_id"],
                "state": "active",
                "members": members,
                "expires_at": session_expires,
                "resume_instruction": "Persist session_token securely. Resume with session_poll after_seq using the last acknowledged seq. The initiator has a separate credential; never exchange credentials between agents.",
            }
            if issued_actor_token is not None:
                result["actor_token"] = issued_actor_token
            return result

    def _require_session(self, connection: sqlite3.Connection, session_token: str, agent_id: str) -> sqlite3.Row:
        """Validate session in a write context; expires stale sessions atomically."""
        return self._require_session_impl(connection, session_token, agent_id, expire_if_stale=True)

    def _require_session_read(self, connection: sqlite3.Connection, session_token: str, agent_id: str) -> sqlite3.Row:
        """Read-safe session validation: raises on invalid/expired but never writes."""
        return self._require_session_impl(connection, session_token, agent_id, expire_if_stale=False)

    def _require_session_impl(self, connection: sqlite3.Connection, session_token: str, agent_id: str, *, expire_if_stale: bool) -> sqlite3.Row:
        token_hash = self._token_hash(session_token)
        _validate_id(agent_id, "agent_id")
        row = connection.execute(
            """
            SELECT s.*, c.agent_id AS credential_agent,
                   c.expires_at AS credential_expires_at,
                   COALESCE(cursor.last_ack_seq, 0) AS credential_last_ack_seq
            FROM sessions AS s
            JOIN session_credentials AS c ON c.session_id = s.session_id
            LEFT JOIN session_cursors AS cursor
              ON cursor.session_id = s.session_id AND cursor.agent_id = c.agent_id
            WHERE c.token_hash = ? AND c.revoked_at IS NULL
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            raise WeftError("session_unauthorized", "Session token is invalid")
        if row["state"] != "active":
            raise WeftError("session_closed", f"Session is {row['state']}")
        if row["expires_at"] < _epoch() or row["credential_expires_at"] < _epoch():
            if expire_if_stale:
                connection.execute("UPDATE sessions SET state = 'expired', closed_at = ? WHERE session_id = ?", (_utc_now(), row["session_id"]))
            raise WeftError("session_expired", "Session has expired; create a new pairing")
        if agent_id != row["credential_agent"]:
            raise WeftError("session_forbidden", "Session credential is bound to a different agent")
        return row

    @staticmethod
    def _session_event_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "protocol": WEFT_PROTOCOL,
            "version": WEFT_VERSION,
            "session_id": row["session_id"],
            "seq": row["seq"],
            "event_id": row["event_id"],
            "origin_agent": row["origin_agent"],
            "type": row["kind"],
            "payload": _parse_json(row["payload_json"], None),
            "trace_id": row["trace_id"],
            "timestamp": row["created_at"],
        }

    def session_send(
        self,
        session_token: str,
        agent_id: str,
        kind: str,
        payload: Any,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_.:-]{1,63}", kind):
            raise WeftError("invalid_message_type", "kind must be a lowercase namespaced event type")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 160:
            raise WeftError("invalid_argument", "idempotency_key must be 8-160 characters")
        payload_json = _json(payload)
        with self._transaction() as connection:
            session = self._require_session(connection, session_token, agent_id)
            seq = int(session["cursor_head"]) + 1
            event_id = _new_id("event")
            created_at = _utc_now()
            inserted = connection.execute(
                """
                INSERT INTO session_events(event_id, session_id, seq, origin_agent, kind, payload_json, idempotency_key, trace_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, origin_agent, idempotency_key) DO NOTHING
                """,
                (event_id, session["session_id"], seq, agent_id, kind, payload_json, idempotency_key, trace_id, created_at),
            ).rowcount
            if inserted == 0:
                duplicate = connection.execute(
                    "SELECT * FROM session_events WHERE session_id = ? AND origin_agent = ? AND idempotency_key = ?",
                    (session["session_id"], agent_id, idempotency_key),
                ).fetchone()
                assert duplicate is not None
                return {"sent": False, "idempotent": True, "event": self._session_event_dict(duplicate)}
            connection.execute("UPDATE sessions SET cursor_head = ?, last_activity = ? WHERE session_id = ?", (seq, _epoch(), session["session_id"]))
            return {
                "sent": True,
                "event": {
                    "protocol": WEFT_PROTOCOL,
                    "version": WEFT_VERSION,
                    "session_id": session["session_id"],
                    "seq": seq,
                    "event_id": event_id,
                    "origin_agent": agent_id,
                    "type": kind,
                    "payload": _parse_json(payload_json, None),
                    "trace_id": trace_id,
                    "timestamp": created_at,
                },
            }

    def session_poll(self, session_token: str, agent_id: str, after_seq: int = 0, limit: int = 100) -> dict[str, Any]:
        after_seq = _safe_int(after_seq, 0, 0, 2**63 - 1)
        limit = _safe_int(limit, 100, 1, 200)
        # Polling is read-heavy and must not take SQLite's BEGIN IMMEDIATE
        # writer lock. Sends and acknowledgements remain transactional; a
        # consumer's activity is represented by its explicit ACK.
        with self._read() as connection:
            session = self._require_session_read(connection, session_token, agent_id)
            rows = connection.execute("SELECT * FROM session_events WHERE session_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?", (session["session_id"], after_seq, limit)).fetchall()
            events = [self._session_event_dict(row) for row in rows]
            next_seq = events[-1]["seq"] if events else after_seq
            return {"session_id": session["session_id"], "state": session["state"], "events": events, "next_seq": next_seq, "cursor_head": session["cursor_head"], "last_ack_seq": session["credential_last_ack_seq"], "has_more": len(events) == limit}

    def session_wait(self, session_token: str, agent_id: str, after_seq: int = 0, timeout_seconds: int = 20, limit: int = 100) -> dict[str, Any]:
        timeout_seconds = _safe_int(timeout_seconds, 20, 0, 30)
        deadline = time.monotonic() + timeout_seconds
        result = self.session_poll(session_token, agent_id, after_seq, limit)
        while not result["events"] and time.monotonic() < deadline:
            time.sleep(0.25)
            result = self.session_poll(session_token, agent_id, after_seq, limit)
        result["timed_out"] = not bool(result["events"])
        return result

    def session_ack(self, session_token: str, agent_id: str, seq: int) -> dict[str, Any]:
        seq = _safe_int(seq, 0, 0, 2**63 - 1)
        with self._transaction() as connection:
            session = self._require_session(connection, session_token, agent_id)
            if seq > session["cursor_head"]:
                raise WeftError("invalid_cursor", "Cannot acknowledge an event beyond the session head")
            cursor = connection.execute(
                """
                INSERT INTO session_cursors(session_id, agent_id, last_ack_seq, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, agent_id) DO UPDATE
                SET last_ack_seq = MAX(last_ack_seq, excluded.last_ack_seq),
                    updated_at = excluded.updated_at
                RETURNING last_ack_seq
                """,
                (session["session_id"], agent_id, seq, _utc_now()),
            ).fetchone()
            assert cursor is not None
            return {"session_id": session["session_id"], "agent_id": agent_id, "last_ack_seq": cursor["last_ack_seq"]}

    def session_status(self, session_token: str, agent_id: str) -> dict[str, Any]:
        with self._read() as connection:
            session = self._require_session_read(connection, session_token, agent_id)
            cursors = connection.execute("SELECT agent_id, last_ack_seq FROM session_cursors WHERE session_id = ? ORDER BY agent_id", (session["session_id"],)).fetchall()
            return {"session_id": session["session_id"], "team_id": session["team_id"], "state": session["state"], "members": [session["agent_a"], session["agent_b"]], "cursor_head": session["cursor_head"], "cursors": [{"agent_id": row["agent_id"], "last_ack_seq": row["last_ack_seq"]} for row in cursors], "expires_at": session["expires_at"], "last_activity": session["last_activity"]}

    def close_session(self, session_token: str, agent_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            session = self._require_session(connection, session_token, agent_id)
            connection.execute("UPDATE sessions SET state = 'closed', closed_at = ?, last_activity = ? WHERE session_id = ?", (_utc_now(), _epoch(), session["session_id"]))
            self._insert_event(connection, session["team_id"], "session.closed", agent_id, session["session_id"], {})
            return {"session_id": session["session_id"], "state": "closed"}

    def prune_expired(self, retention_seconds: int = 2_592_000, apply: bool = False) -> dict[str, Any]:
        """Preview or apply bounded cleanup for terminal coordination history.

        Cleanup is deliberately operator-invoked rather than automatic. Tasks,
        messages, and active sessions are never removed by this operation.
        """
        retention_seconds = _safe_int(retention_seconds, 2_592_000, 86_400, 315_360_000)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=retention_seconds)
        cutoff_text = cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        now = _epoch()
        with self._transaction() as connection:
            if apply:
                connection.execute(
                    "UPDATE sessions SET state = 'expired', closed_at = ? WHERE state = 'active' AND expires_at < ?",
                    (_utc_now(), now),
                )
            counts = {
                "terminal_sessions": connection.execute("SELECT COUNT(*) FROM sessions WHERE state IN ('closed', 'expired') AND created_at < ?", (cutoff_text,)).fetchone()[0],
                "audit_events": connection.execute("SELECT COUNT(*) FROM events WHERE created_at < ?", (cutoff_text,)).fetchone()[0],
                "pairing_credentials": connection.execute("SELECT COUNT(*) FROM pairing_credentials WHERE created_at < ?", (cutoff_text,)).fetchone()[0],
            }
            deleted = {key: 0 for key in counts}
            if apply:
                deleted["terminal_sessions"] = connection.execute("DELETE FROM sessions WHERE state IN ('closed', 'expired') AND created_at < ?", (cutoff_text,)).rowcount
                deleted["audit_events"] = connection.execute("DELETE FROM events WHERE created_at < ?", (cutoff_text,)).rowcount
                deleted["pairing_credentials"] = connection.execute("DELETE FROM pairing_credentials WHERE created_at < ?", (cutoff_text,)).rowcount
            return {"applied": apply, "cutoff": cutoff_text, "would_delete": counts, "deleted": deleted}

    def health_status(self) -> dict[str, Any]:
        with self._read() as connection:
            connection.execute("SELECT 1").fetchone()
            sessions = connection.execute("SELECT COUNT(*) FROM sessions WHERE state = 'active' AND expires_at >= ?", (_epoch(),)).fetchone()[0]
            schema = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
            return {"status": "ok", "service": "weft", "protocol": WEFT_PROTOCOL, "version": WEFT_VERSION, "schema_version": int(schema["value"]) if schema else None, "active_sessions": sessions}

    def team_status(
        self,
        team_id: str,
        include_events: bool = False,
        agent_id: str | None = None,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        _validate_id(team_id, "team_id")
        with self._transaction() as connection:
            self._authorize_optional_actor(connection, team_id, agent_id, actor_token)
            self._ensure_team(connection, team_id)
            self._recover_expired_leases(connection, team_id)
            team = connection.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()
            agents = connection.execute("SELECT * FROM agents WHERE team_id = ? ORDER BY agent_id", (team_id,)).fetchall()
            tasks = connection.execute("SELECT * FROM tasks WHERE team_id = ? ORDER BY priority ASC, updated_at DESC", (team_id,)).fetchall()
            result: dict[str, Any] = {
                "team_id": team_id,
                "name": team["name"],
                "agents": [self._agent_dict(row, self.heartbeat_timeout) for row in agents],
                "tasks": [self._task_dict(row) for row in tasks],
                "counts": {
                    "agents": len(agents),
                    "tasks": len(tasks),
                    "open_tasks": sum(row["status"] not in {"done", "cancelled"} for row in tasks),
                },
            }
            if include_events:
                events = connection.execute("SELECT * FROM events WHERE team_id = ? ORDER BY created_at DESC LIMIT 100", (team_id,)).fetchall()
                result["events"] = [
                    {"event_id": row["event_id"], "type": row["event_type"], "actor_id": row["actor_id"], "object_id": row["object_id"], "payload": _parse_json(row["payload_json"], {}), "created_at": row["created_at"]}
                    for row in events
                ]
            return result

    def protocol_info(self) -> dict[str, Any]:
        return {
            "protocol": WEFT_PROTOCOL,
            "version": WEFT_VERSION,
            "mcp_protocol": MCP_PROTOCOL_VERSION,
            "tagline": "Connect two agents. Get a team.",
            "guarantees": [
                "idempotent task and message mutations",
                "atomic task claims with fencing tokens",
                "workspace-contained file scopes",
                "evidence-aware quality gates",
                "append-only coordination audit events",
                "single-use pairing capabilities with hashed-at-rest tokens",
                "ordered, resumable session events with explicit acknowledgements",
            ],
            "boundaries": [
                "Weft does not silently substitute a requested model",
                "Weft never executes arbitrary shell commands from a tool payload",
                "message payloads are untrusted data and are never treated as instructions by the server",
                "a link can bootstrap an MCP-capable host but cannot install software into an arbitrary non-MCP product",
            ],
        }

    def model_catalog(self) -> dict[str, Any]:
        return {"models": [dict(slot) for slot in MODEL_SLOTS], "selection_policy": "capability-first; requested model is recorded and never silently replaced"}
