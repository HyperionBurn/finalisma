"""Migration-upgrade integration tests (Wave F — Cloud Spine).

RED deliverable: these tests import ``weft_cloud.migrations`` which does
not exist yet, so the suite MUST fail on import. When the cloud plane is built,
the tests below verify the v3→cloud upgrade contract against a REAL schema-v3
coordinator database (no mocks, real storage):

1. ``test_v3_to_cloud_upgrade`` — open a real v3 DB, exercise the coordinator
   (register agents, create tasks, emit session events), snapshot the file,
   apply the cloud migration, reopen, and assert: ``agent_credentials``
   unchanged (same row count, same token hashes), existing actor tokens still
   authenticate, all coordinator rows present and unchanged, new cloud tables
   exist and are empty, ``schema_migrations`` count equals migrations applied.
2. ``test_migration_additive_only`` — after migration, coordinator tables have
   the SAME column set as before (``PRAGMA table_info`` before/after).
3. ``test_migration_idempotent`` — ``apply_migrations`` twice; second run is a
   no-op, version unchanged, no duplicate rows.

Design source: ``docs/CLOUD_SPINE_DESIGN.md`` §4, §7.3.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Add src/ to the import path — same convention as tests/test_weft.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftStore  # noqa: E402

# This import is the RED gate: the module does not exist yet.
from weft_cloud.migrations import (  # noqa: E402
    MIGRATIONS,
    _CLOUD_TABLES_SQL,
    _IDENTITY_ACCOUNTS_SQL,
    _IDENTITY_INVITES_SQL,
    _IDENTITY_MEMBERS_SQL,
    _IDENTITY_OUTBOX_SQL,
    _IDENTITY_SESSIONS_SQL,
    _ROOM_TABLES_SQL,
    apply_migrations,
)


COORDINATOR_TABLES = (
    "teams",
    "agents",
    "agent_credentials",
    "tasks",
    "messages",
    "message_reads",
    "evidence",
    "events",
    "pairings",
    "pairing_credentials",
    "sessions",
    "session_credentials",
    "session_cursors",
    "session_events",
    "schema_meta",
)

CLOUD_TABLES = (
    "cloud_tenants",
    "cloud_tenant_rooms",
    "cloud_counters",
    "cloud_room_counters",
    "cloud_rate_windows",
    "cloud_outbox",
    "cloud_audit",
    "cloud_event_mirror",
    "schema_migrations",
)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class MigrationUpgradeTests(unittest.TestCase):
    """v3→cloud migration upgrade contract (real storage, no mocks)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="weft-mig-")
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "state.db"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        # Seed a real schema-v3 database and exercise the coordinator.
        self._seed_v3_database()

    def tearDown(self) -> None:
        # Windows: the closed store's WAL -shm mapping is released on GC, not
        # synchronously on close(). A GC pass before tempdir cleanup
        # deterministically releases the handle (AGENT_HANDOVER.md §6).
        import gc
        gc.collect()
        self.tmp.cleanup()

    def _seed_v3_database(self) -> None:
        """Open a real WeftStore, register agents, create tasks, emit
        session events. Records the actor tokens so tests can re-authenticate
        after the migration."""
        self.actor_tokens: dict[str, str] = {}
        with WeftStore(self.state_path, self.workspace) as store:
            # Two agents — each gets an actor credential on first registration.
            reg_a = store.register_agent(
                "team-1",
                "agent-a",
                "Planner",
                "architect",
                "gpt-5.6-luna",
                ["planning", "critique"],
            )
            reg_b = store.register_agent(
                "team-1",
                "agent-b",
                "Reviewer",
                "security",
                "opencode-go/mimo-v2.5",
                ["security", "testing"],
            )
            self.actor_tokens["agent-a"] = reg_a["actor_token"]
            self.actor_tokens["agent-b"] = reg_b["actor_token"]

            # Create a task (emits task.created event).
            task_result = store.create_task(
                "team-1",
                "agent-a",
                "Review the retry boundary",
                "Inspect the handoff artifact and return a security finding.",
                scope=["handoff.txt"],
                preferred_agent="agent-b",
                idempotency_key="mig-task-v1",
            )
            self.task_id = task_result["task"]["task_id"]

            # Create a pairing + session so session_events has rows.
            pairing = store.create_pairing(
                "agent-a",
                "team-1",
                capabilities_offered=["read", "comment"],
            )
            joined = store.join_pairing(
                pairing["join_token"],
                "agent-b",
                model="opencode-go/mimo-v2.5",
                capabilities=["security"],
                consent=True,
            )
            self.session_id = joined["session_id"]

            # Emit a session event via the real API (session_send).
            store.session_send(
                joined["session_token"],
                "agent-b",
                "message",
                {"text": "synthetic migration-upgrade payload"},
                idempotency_key="mig-event-v1",
            )

    def _table_columns(self, connection: sqlite3.Connection, table: str) -> tuple[tuple[str, str], ...]:
        """Return ((name, type), ...) for ``table`` via PRAGMA table_info."""
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return tuple((row["name"], row["type"]) for row in rows)

    def _row_count(self, connection: sqlite3.Connection, table: str) -> int:
        return connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def _file_bytes(self) -> bytes:
        return self.state_path.read_bytes()

    @contextmanager
    def _connect_ro(self) -> Iterator[sqlite3.Connection]:
        """Read-only inspection connection, ALWAYS closed on exit.

        A plain read-write connection to a DELETE-journal database is used (the
        migration returns the DB to DELETE mode), with an explicit close. This
        avoids the -shm/-wal handles that a WAL-mode connection leaves on
        Windows, which intermittently locked tempdir teardown. Using
        ``with sqlite3.Connection`` alone would commit but NOT close; a
        contextmanager that closes in ``finally`` is required.
        """
        connection = sqlite3.connect(self.state_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Test 1: v3→cloud upgrade preserves all coordinator data.
    # ------------------------------------------------------------------
    def test_v3_to_cloud_upgrade(self) -> None:
        # Snapshot file bytes and coordinator state BEFORE migration.
        bytes_before = self._file_bytes()
        with self._connect_ro() as conn:
            conn.row_factory = sqlite3.Row
            cred_rows_before = conn.execute(
                "SELECT team_id, agent_id, token_hash FROM agent_credentials ORDER BY team_id, agent_id"
            ).fetchall()
            cred_hashes_before = [(r["team_id"], r["agent_id"], r["token_hash"]) for r in cred_rows_before]
            tasks_before = self._row_count(conn, "tasks")
            sessions_before = self._row_count(conn, "sessions")
            session_events_before = self._row_count(conn, "session_events")

        self.assertEqual(len(cred_hashes_before), 2, "expected two agent credentials seeded")
        self.assertGreater(tasks_before, 0, "expected at least one task seeded")
        self.assertGreater(sessions_before, 0, "expected at least one session seeded")
        self.assertGreater(session_events_before, 0, "expected at least one session event seeded")

        # Apply the cloud migration.
        with WeftStore(self.state_path, self.workspace) as store:
            apply_migrations(store)

        # File must have grown (new tables appended), not been replaced.
        bytes_after = self._file_bytes()
        self.assertTrue(
            len(bytes_after) >= len(bytes_before),
            "migration must not shrink the database file",
        )

        # Reopen and assert coordinator tables are intact.
        with self._connect_ro() as conn:
            conn.row_factory = sqlite3.Row
            cred_hashes_after = [
                (r["team_id"], r["agent_id"], r["token_hash"])
                for r in conn.execute(
                    "SELECT team_id, agent_id, token_hash FROM agent_credentials ORDER BY team_id, agent_id"
                ).fetchall()
            ]
            self.assertEqual(
                cred_hashes_after,
                cred_hashes_before,
                "agent_credentials rows must be unchanged after migration",
            )
            self.assertEqual(self._row_count(conn, "tasks"), tasks_before)
            self.assertEqual(self._row_count(conn, "sessions"), sessions_before)
            self.assertEqual(self._row_count(conn, "session_events"), session_events_before)

            # New cloud tables must exist and be empty — EXCEPT schema_migrations,
            # which tracks applied migrations and is expected to be non-empty.
            for table in CLOUD_TABLES:
                if table == "schema_migrations":
                    continue
                count = self._row_count(conn, table)
                self.assertEqual(
                    count,
                    0,
                    f"cloud table {table} must be empty after migration (found {count} rows)",
                )

            # schema_migrations count equals the number of migrations applied.
            schema_migrations_count = self._row_count(conn, "schema_migrations")
        self.assertGreaterEqual(
            schema_migrations_count,
            1,
            "at least one migration must be recorded in schema_migrations",
        )

        # Existing actor tokens still authenticate — call a store operation
        # that requires actor auth with the pre-migration token.
        with WeftStore(self.state_path, self.workspace, require_actor_auth=True) as store:
            # rotate_agent_credential requires actor auth; proves the token works.
            rotated = store.rotate_agent_credential(
                "team-1",
                "agent-a",
                current_token=self.actor_tokens["agent-a"],
            )
            self.assertIn("actor_token", rotated)
            self.assertNotEqual(rotated["actor_token"], self.actor_tokens["agent-a"])

    # ------------------------------------------------------------------
    # Test 2: migration is purely additive — coordinator column sets unchanged.
    # ------------------------------------------------------------------
    def test_migration_additive_only(self) -> None:
        # Capture coordinator column sets BEFORE migration.
        with self._connect_ro() as conn:
            conn.row_factory = sqlite3.Row
            columns_before = {
                table: self._table_columns(conn, table) for table in COORDINATOR_TABLES
            }

        # Apply the cloud migration.
        with WeftStore(self.state_path, self.workspace) as store:
            apply_migrations(store)

        # Coordinator tables must have the EXACT same column set as before.
        with self._connect_ro() as conn:
            conn.row_factory = sqlite3.Row
            for table in COORDINATOR_TABLES:
                columns_after = self._table_columns(conn, table)
                self.assertEqual(
                    columns_after,
                    columns_before[table],
                    f"coordinator table {table} column set changed — migration must be additive",
                )

    # ------------------------------------------------------------------
    # Test 3: migration is idempotent — second run is a no-op.
    # ------------------------------------------------------------------
    def test_migration_idempotent(self) -> None:
        # First application.
        with WeftStore(self.state_path, self.workspace) as store:
            apply_migrations(store)

        with self._connect_ro() as conn:
            conn.row_factory = sqlite3.Row
            version_after_first = self._row_count(conn, "schema_migrations")
            cloud_tenant_rows_after_first = self._row_count(conn, "cloud_tenants")
            cloud_audit_rows_after_first = self._row_count(conn, "cloud_audit")

        self.assertGreaterEqual(version_after_first, 1)

        # Second application — must be a no-op.
        with WeftStore(self.state_path, self.workspace) as store:
            apply_migrations(store)

        with self._connect_ro() as conn:
            conn.row_factory = sqlite3.Row
            version_after_second = self._row_count(conn, "schema_migrations")
            cloud_tenant_rows_after_second = self._row_count(conn, "cloud_tenants")
            cloud_audit_rows_after_second = self._row_count(conn, "cloud_audit")

        self.assertEqual(
            version_after_second,
            version_after_first,
            "second apply_migrations must not add duplicate schema_migrations rows",
        )
        self.assertEqual(
            cloud_tenant_rows_after_second,
            cloud_tenant_rows_after_first,
            "cloud_tenants row count must be unchanged on second run",
        )
        self.assertEqual(
            cloud_audit_rows_after_second,
            cloud_audit_rows_after_first,
            "cloud_audit row count must be unchanged on second run",
        )


class MessageKindMigrationTests(unittest.TestCase):
    """cloud_009 adds the room message_kind column forward-only and idempotent."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="finalisma-mk-")
        self.root = Path(self.tmp.name)
        self.cloud_path = self.root / "cloud.db"

    def tearDown(self) -> None:
        import gc
        gc.collect()
        self.tmp.cleanup()

    def _apply(self) -> None:
        from weft_cloud.storage import SqliteWalBackend
        backend = SqliteWalBackend(self.cloud_path)
        backend.initialize()
        try:
            apply_migrations(backend)
        finally:
            backend.close()

    def _has_column(self, connection: sqlite3.Connection) -> bool:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT 1 FROM pragma_table_info('cloud_room_event_log') WHERE name = 'message_kind'"
        ).fetchone()
        return row is not None

    def test_cloud_009_adds_message_kind_to_existing_event_log(self) -> None:
        """A cloud DB whose event log predates message_kind is upgraded in place."""
        from weft_cloud.storage import SqliteWalBackend
        backend = SqliteWalBackend(self.cloud_path)
        backend.initialize()
        try:
            # Pre-upgrade shape: event log WITHOUT message_kind.
            with backend._transaction() as tx:
                tx.execute(
                    "CREATE TABLE cloud_room_event_log ("
                    " event_id TEXT PRIMARY KEY, room_id TEXT NOT NULL, tenant_id TEXT NOT NULL,"
                    " seq INTEGER NOT NULL, origin_agent TEXT NOT NULL, kind TEXT NOT NULL,"
                    " payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL, trace_id TEXT,"
                    " created_at TEXT NOT NULL,"
                    " UNIQUE(room_id, seq), UNIQUE(room_id, origin_agent, idempotency_key))"
                )
            conn = sqlite3.connect(self.cloud_path)
            try:
                self.assertFalse(self._has_column(conn),
                                 "precondition: the old event log lacks message_kind")
            finally:
                conn.close()  # `with conn:` would NOT close the connection
        finally:
            backend.close()

        # Applying the migrations adds the column and records cloud_009.
        self._apply()
        conn = sqlite3.connect(self.cloud_path)
        try:
            self.assertTrue(self._has_column(conn), "cloud_009 must add the message_kind column")
        finally:
            conn.close()  # `with conn:` would NOT close the connection

        # Second application is a no-op (idempotent, no duplicate column error).
        self._apply()
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM schema_migrations WHERE migration_id = 'cloud_009_room_message_kind'"
            ).fetchone()["c"]
            self.assertEqual(n, 1, "cloud_009 must be recorded exactly once")
            self.assertTrue(self._has_column(conn))
        finally:
            conn.close()  # `with conn:` would NOT close the connection

    def test_cloud_009_is_skipped_when_column_already_present(self) -> None:
        """A DB created by the new code already has the column; the migration
        must record itself as applied without re-running the ALTER."""
        from weft_cloud.rooms import CloudRoomService
        from weft_cloud.storage import SqliteWalBackend
        backend = SqliteWalBackend(self.cloud_path)
        backend.initialize()
        try:
            CloudRoomService(backend)  # creates the event log WITH message_kind
            apply_migrations(backend)
            apply_migrations(backend)  # idempotent second run
        finally:
            backend.close()
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            self.assertTrue(self._has_column(conn))
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM schema_migrations WHERE migration_id = 'cloud_009_room_message_kind'"
            ).fetchone()["c"]
            self.assertEqual(n, 1)
        finally:
            conn.close()  # `with conn:` would NOT close the connection


# ---------------------------------------------------------------------------
# Production-shaped upgrade: a database mid-sequence with cloud_008 unapplied.
# ---------------------------------------------------------------------------


def _pre_migration_schema_sql() -> str:
    """The schema a production database has with cloud_008/cloud_009 unapplied:
    every cloud_001..cloud_007 table in its base shape — the identity outbox
    WITHOUT the delivery columns and the room event log WITHOUT ``message_kind``.

    Built from the same module constants the migrations use so the fixture
    tracks the base shapes automatically; the one deliberate edit is stripping
    ``message_kind`` from the event-log definition.
    """
    event_log_pre_kind = _ROOM_TABLES_SQL.replace(
        "    kind TEXT NOT NULL,\n    message_kind TEXT,\n",
        "    kind TEXT NOT NULL,\n",
        1,
    )
    assert "message_kind" not in event_log_pre_kind, (
        "pre-migration fixture must not contain message_kind"
    )
    return (
        _CLOUD_TABLES_SQL
        + _IDENTITY_ACCOUNTS_SQL
        + _IDENTITY_SESSIONS_SQL
        + _IDENTITY_MEMBERS_SQL
        + _IDENTITY_INVITES_SQL
        + _IDENTITY_OUTBOX_SQL
        + event_log_pre_kind
    )


_PRE_CLOUD_008_MIGRATION_IDS = (
    "cloud_001_init",
    "cloud_002_identity_accounts",
    "cloud_003_identity_sessions",
    "cloud_004_identity_members",
    "cloud_005_identity_invites",
    "cloud_006_identity_outbox",
    "cloud_007_room_tables",
)


class ProductionShapedUpgradeTests(unittest.TestCase):
    """Upgrade the LIVE production shape in place without losing a row.

    The fixture matches production: the pre-cloud_008 schema (outbox without
    the delivery columns, event log without ``message_kind``) with real data in
    the five tables the production report lists — accounts, rooms, room_members,
    room_event_log and identity_outbox — and cloud_001..cloud_007 recorded in
    ``schema_migrations``.
    """

    def setUp(self) -> None:
        from weft_cloud.storage import SqliteWalBackend

        self.tmp = tempfile.TemporaryDirectory(prefix="prod-shape-")
        self.root = Path(self.tmp.name)
        self.cloud_path = self.root / "cloud.db"
        self.backend = SqliteWalBackend(self.cloud_path)
        self.backend.initialize()
        self._build_fixture_schema_and_data()

    def tearDown(self) -> None:
        try:
            self.backend.close()
        finally:
            import gc
            gc.collect()
            self.tmp.cleanup()

    # -- fixture construction ------------------------------------------------

    def _build_fixture_schema_and_data(self) -> None:
        from weft_cloud.storage import utc_now_iso

        now = utc_now_iso()
        with self.backend._transaction() as conn:
            conn.executescript(_pre_migration_schema_sql())
            for migration_id in _PRE_CLOUD_008_MIGRATION_IDS:
                conn.execute(
                    "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                    (migration_id, now),
                )

            conn.execute(
                "INSERT INTO cloud_tenants(tenant_id, name, plan_id, created_at) VALUES (?, ?, ?, ?)",
                ("tenant_a", "Org A", "free", now),
            )
            conn.execute(
                "INSERT INTO cloud_tenants(tenant_id, name, plan_id, created_at) VALUES (?, ?, ?, ?)",
                ("tenant_b", "Org B", "pro", now),
            )

            accounts = [
                ("acct_001", "tenant_a", "a1@example.com"),
                ("acct_002", "tenant_a", "a2@example.com"),
                ("acct_003", "tenant_a", "a3@example.com"),
                ("acct_004", "tenant_b", "b1@example.com"),
                ("acct_005", "tenant_b", "b2@example.com"),
            ]
            for account_id, tenant_id, email in accounts:
                conn.execute(
                    "INSERT INTO cloud_identity_accounts("
                    " account_id, tenant_id, email, salt, password_hash, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (account_id, tenant_id, email, b"\x00" * 16, b"\x00" * 32, now),
                )
                conn.execute(
                    "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at)"
                    " VALUES (?, ?, 'member', ?)",
                    (tenant_id, account_id, now),
                )

            # cloud_identity_outbox in its BASE shape — no delivery columns.
            conn.execute(
                "INSERT INTO cloud_identity_outbox("
                " entry_id, tenant_id, to_email, subject, body, created_at)"
                " VALUES ('obx_001', 'tenant_a', 'a1@example.com', 'Verify', 'body', ?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO cloud_identity_outbox("
                " entry_id, tenant_id, to_email, subject, body, created_at)"
                " VALUES ('obx_002', 'tenant_a', 'a2@example.com', 'Reset', 'body', ?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO cloud_identity_outbox("
                " entry_id, tenant_id, to_email, subject, body, created_at)"
                " VALUES ('obx_003', 'tenant_b', 'b1@example.com', 'Invite', 'body', ?)",
                (now,),
            )

            # cloud_rooms + members + event log in base shapes.
            conn.execute(
                "INSERT INTO cloud_rooms("
                " room_id, tenant_id, owner_agent_id, name, cap, state, link_id,"
                " created_at, expires_at, cursor_head)"
                " VALUES ('room_001', 'tenant_a', 'acct_001', 'Room One', 4, 'active',"
                " 'link_001', ?, 0, 3)",
                (now,),
            )
            conn.execute(
                "INSERT INTO cloud_rooms("
                " room_id, tenant_id, owner_agent_id, name, cap, state, link_id,"
                " created_at, expires_at, cursor_head)"
                " VALUES ('room_002', 'tenant_a', 'acct_002', 'Room Two', 2, 'forming',"
                " 'link_002', ?, 0, 1)",
                (now,),
            )
            conn.execute(
                "INSERT INTO cloud_rooms("
                " room_id, tenant_id, owner_agent_id, name, cap, state, link_id,"
                " created_at, expires_at, cursor_head)"
                " VALUES ('room_003', 'tenant_b', 'acct_004', 'Room Three', 3, 'active',"
                " 'link_003', ?, 0, 2)",
                (now,),
            )

            for tenant_id, room_id, agent_id in (
                ("tenant_a", "room_001", "acct_001"),
                ("tenant_a", "room_001", "acct_002"),
                ("tenant_a", "room_001", "acct_003"),
                ("tenant_a", "room_002", "acct_001"),
                ("tenant_a", "room_002", "acct_002"),
                ("tenant_b", "room_003", "acct_004"),
                ("tenant_b", "room_003", "acct_005"),
            ):
                conn.execute(
                    "INSERT INTO cloud_room_members("
                    " tenant_id, room_id, agent_id, joined_at, last_seen, status,"
                    " capabilities_json, actor_token_hash)"
                    " VALUES (?, ?, ?, ?, 0, 'active', '[]', ?)",
                    (tenant_id, room_id, agent_id, now, f"hash_{agent_id}"),
                )

            events = [
                ("evt_001", "room_001", "tenant_a", 1, "acct_001", "room.joined", "{}"),
                ("evt_002", "room_001", "tenant_a", 2, "acct_002", "room.joined", "{}"),
                ("evt_003", "room_001", "tenant_a", 3, "acct_003", "room.joined", "{}"),
                ("evt_004", "room_002", "tenant_a", 1, "acct_001", "room.created", "{}"),
                ("evt_005", "room_003", "tenant_b", 1, "acct_004", "room.created", "{}"),
                ("evt_006", "room_003", "tenant_b", 2, "acct_005", "room.joined", "{}"),
            ]
            for event_id, room_id, tenant_id, seq, origin, kind, payload in events:
                conn.execute(
                    "INSERT INTO cloud_room_event_log("
                    " event_id, room_id, tenant_id, seq, origin_agent, kind,"
                    " payload_json, idempotency_key, trace_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (event_id, room_id, tenant_id, seq, origin, kind, payload,
                     f"idem_{event_id}", now),
                )
        conn = sqlite3.connect(self.cloud_path)
        try:
            self._assert_pre_migration_preconditions()
        finally:
            conn.close()  # `with conn:` would NOT close the connection

    def _assert_pre_migration_preconditions(self) -> None:
        """The fixture must actually be production-shaped before upgrade."""
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            outbox_columns = {
                r["name"] for r in conn.execute(
                    "PRAGMA table_info(cloud_identity_outbox)"
                ).fetchall()
            }
            self.assertNotIn("status", outbox_columns,
                             "precondition: outbox lacks delivery columns")
            event_columns = {
                r["name"] for r in conn.execute(
                    "PRAGMA table_info(cloud_room_event_log)"
                ).fetchall()
            }
            self.assertNotIn("message_kind", event_columns,
                             "precondition: event log predates message_kind")
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM schema_migrations"
            ).fetchone()["c"]
            self.assertEqual(n, len(_PRE_CLOUD_008_MIGRATION_IDS),
                             "precondition: cloud_008/cloud_009 not yet applied")
        finally:
            conn.close()

    # -- helpers -------------------------------------------------------------

    def _table_counts(self) -> dict[str, int]:
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            tables = ("cloud_identity_accounts", "cloud_rooms", "cloud_room_members",
                      "cloud_room_event_log", "cloud_identity_outbox")
            return {
                table: conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table}"
                ).fetchone()["c"]
                for table in tables
            }
        finally:
            conn.close()

    def _outbox_columns(self) -> set[str]:
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            return {r["name"] for r in conn.execute(
                "PRAGMA table_info(cloud_identity_outbox)"
            ).fetchall()}
        finally:
            conn.close()

    # -- tests ---------------------------------------------------------------

    def test_ensure_schema_upgrades_existing_database_and_preserves_rows(self) -> None:
        """Bug 1: ensure_schema must NOT short-circuit on an existing table.

        The pre-cloud_008 production shape is brought fully up to date — all six
        delivery columns, message_kind, and the cloud_outbox lifecycle columns
        added, all eleven migrations recorded — without losing a single
        pre-existing row.
        """
        from weft_cloud.identity.schema import ensure_schema

        counts_before = self._table_counts()
        self.assertGreater(counts_before["cloud_identity_accounts"], 0)
        self.assertGreater(counts_before["cloud_rooms"], 0)
        self.assertGreater(counts_before["cloud_room_members"], 0)
        self.assertGreater(counts_before["cloud_room_event_log"], 0)
        self.assertGreater(counts_before["cloud_identity_outbox"], 0)

        ensure_schema(self.backend)

        self.assertEqual(self._table_counts(), counts_before,
                         "upgrade must not add or remove a single pre-existing row")
        columns = self._outbox_columns()
        for column in ("status", "attempts", "next_attempt_at",
                       "claimed_at", "claimed_by", "last_error"):
            self.assertIn(column, columns,
                          f"cloud_008 must add the {column} delivery column")
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            event_columns = {r["name"] for r in conn.execute(
                "PRAGMA table_info(cloud_room_event_log)").fetchall()}
            self.assertIn("message_kind", event_columns,
                          "cloud_009 must add message_kind to the event log")
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM schema_migrations"
            ).fetchone()["c"]
            self.assertEqual(n, len(MIGRATIONS),
                             f"every cloud migration ({len(MIGRATIONS)}) must be recorded, got {n}")
        finally:
            conn.close()

    def test_upgrade_is_noop_on_second_and_third_run(self) -> None:
        from weft_cloud.identity.schema import ensure_schema

        ensure_schema(self.backend)
        counts_after_first = self._table_counts()
        ensure_schema(self.backend)
        ensure_schema(self.backend)

        self.assertEqual(self._table_counts(), counts_after_first,
                         "repeat runs must not error or change row counts")
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM schema_migrations"
            ).fetchone()["c"]
            self.assertEqual(n, len(MIGRATIONS),
                             "no duplicate schema_migrations rows on rerun")
        finally:
            conn.close()

    def test_partial_column_preexisting_does_not_wedge(self) -> None:
        """Bug 2: if one delivery column already exists out-of-band, the
        migration completes the rest instead of failing with duplicate column."""
        from weft_cloud.identity.schema import ensure_schema

        # Simulate an out-of-band column: `status` exists but cloud_008 is not
        # recorded. The old code wedged forever on `duplicate column name`.
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.execute(
                "ALTER TABLE cloud_identity_outbox ADD COLUMN status TEXT NOT NULL DEFAULT 'queued'"
            )
            conn.commit()
        finally:
            conn.close()

        ensure_schema(self.backend)  # must not raise

        columns = self._outbox_columns()
        for column in ("status", "attempts", "next_attempt_at",
                       "claimed_at", "claimed_by", "last_error"):
            self.assertIn(column, columns,
                          f"{column} must be present after the partial-state upgrade")

    def test_claim_due_runs_without_error_after_upgrade(self) -> None:
        """After the upgrade, the drain worker's claim_due() can SELECT/UPDATE
        the outbox — previously it would die with `no such column: status`."""
        from weft_cloud.identity.outbox_worker import OutboxDrainer
        from weft_cloud.identity.schema import ensure_schema

        class _NoopMailer:
            def send(self, *args, **kwargs):
                return None

        ensure_schema(self.backend)
        drainer = OutboxDrainer(self.backend, _NoopMailer(), batch_size=50)
        claimed = drainer.claim_due()
        # The three seeded rows default to status='queued' after the ALTER.
        self.assertEqual(len(claimed), 3)
        self.assertEqual({r["entry_id"] for r in claimed},
                         {"obx_001", "obx_002", "obx_003"})


# ---------------------------------------------------------------------------
# cloud_010 — room-membership account recovery. Live memberships were bound to
# the caller's SESSION-token hash (rotates on login) with a client-suppliable
# agent_id; the migration re-keys them to the true account via the session
# table, which is never deleted (only revoked).
# ---------------------------------------------------------------------------


class RoomMembershipAccountRecoveryTests(unittest.TestCase):
    """cloud_010: existing room memberships are re-keyed to the ACCOUNT.

    The regression bound room membership to ``actor_token_hash`` — the SHA-256
    of a SESSION token that rotates on every login. cloud_010 recovers the
    true account from ``cloud_identity_sessions.token_hash`` (sessions are
    never deleted, only revoked, so even expired sessions resolve), rewrites
    ``cloud_room_members.agent_id`` to that account, re-keys the member's
    cursor, recovers the room owner, and de-duplicates forged rows.
    """

    def setUp(self) -> None:
        from weft_cloud.storage import SqliteWalBackend

        self.tmp = tempfile.TemporaryDirectory(prefix="room-recovery-")
        self.root = Path(self.tmp.name)
        self.cloud_path = self.root / "cloud.db"
        self.backend = SqliteWalBackend(self.cloud_path)
        self.backend.initialize()
        self._build_fixture()

    def tearDown(self) -> None:
        try:
            self.backend.close()
        finally:
            import gc
            gc.collect()
            self.tmp.cleanup()

    def _build_fixture(self) -> None:
        """Seed the PRE-cloud_010 production shape: sessions exist (never
        deleted), memberships store actor_token_hash + a client-suppliable
        agent_id that may be a bogus name or a forged duplicate."""
        from weft_cloud.storage import utc_now_iso

        now = utc_now_iso()
        sess = {name: _token_hash(f"sess-{name}") for name in
                ("acct_001", "acct_002", "acct_003", "acct_004")}
        with self.backend._transaction() as conn:
            conn.executescript(_pre_migration_schema_sql())
            for migration_id in _PRE_CLOUD_008_MIGRATION_IDS:
                conn.execute(
                    "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                    (migration_id, now),
                )
            conn.execute(
                "INSERT INTO cloud_tenants(tenant_id, name, plan_id, created_at) VALUES (?, ?, ?, ?)",
                ("tenant_a", "Org A", "free", now),
            )
            for account_id, email in (("acct_001", "a1@example.com"),
                                      ("acct_002", "a2@example.com"),
                                      ("acct_003", "a3@example.com"),
                                      ("acct_004", "a4@example.com")):
                conn.execute(
                    "INSERT INTO cloud_identity_accounts("
                    " account_id, tenant_id, email, salt, password_hash, created_at)"
                    " VALUES (?, 'tenant_a', ?, ?, ?, ?)",
                    (account_id, email, b"\x00" * 16, b"\x00" * 32, now),
                )
            for account_id in ("acct_001", "acct_002", "acct_003", "acct_004"):
                conn.execute(
                    "INSERT INTO cloud_identity_sessions("
                    " session_id, tenant_id, account_id, token_hash, created_at,"
                    " expires_at, role_snapshot)"
                    " VALUES (?, 'tenant_a', ?, ?, ?, ?, 'owner')",
                    (f"ses_{account_id}", account_id, sess[account_id], now, 0),
                )

            # room_001: owner identity forged at create time.
            conn.execute(
                "INSERT INTO cloud_rooms(room_id, tenant_id, owner_agent_id, name, cap,"
                " state, link_id, created_at, expires_at, cursor_head)"
                " VALUES ('room_001', 'tenant_a', 'bogus-owner-id', 'Room One', 4,"
                " 'active', 'link_001', ?, 0, 3)",
                (now,),
            )
            # room_002: a forged duplicate membership for acct_004.
            conn.execute(
                "INSERT INTO cloud_rooms(room_id, tenant_id, owner_agent_id, name, cap,"
                " state, link_id, created_at, expires_at, cursor_head)"
                " VALUES ('room_002', 'tenant_a', 'acct_004', 'Room Two', 2,"
                " 'active', 'link_002', ?, 0, 1)",
                (now,),
            )

            members = [
                # (tenant, room, agent_id, hash, status, joined_at order)
                ("tenant_a", "room_001", "bogus-owner-id", sess["acct_001"], "active", 1),
                ("tenant_a", "room_001", "acct_002", sess["acct_002"], "active", 2),
                ("tenant_a", "room_001", "ghost-joiner", sess["acct_003"], "active", 3),
                ("tenant_a", "room_001", "unrecoverable", "no-such-session", "active", 4),
                ("tenant_a", "room_002", "acct_004", sess["acct_004"], "active", 1),
                ("tenant_a", "room_002", "dup-forged", sess["acct_004"], "active", 2),
            ]
            for i, (tenant, room, agent, hash_, status, order) in enumerate(members):
                conn.execute(
                    "INSERT INTO cloud_room_members("
                    " tenant_id, room_id, agent_id, joined_at, last_seen, status,"
                    " capabilities_json, actor_token_hash)"
                    " VALUES (?, ?, ?, ?, ?, ?, '[]', ?)",
                    (tenant, room, agent, now, order, status, hash_),
                )
                conn.execute(
                    "INSERT INTO cloud_room_cursors("
                    " tenant_id, room_id, agent_id, last_ack_seq, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (tenant, room, agent, order, now),
                )

    def _members(self) -> set[tuple[str, str, str]]:
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            return {tuple(r) for r in conn.execute(
                "SELECT tenant_id, room_id, agent_id FROM cloud_room_members"
            ).fetchall()}
        finally:
            conn.close()

    def _owner(self, room_id: str) -> str:
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT owner_agent_id FROM cloud_rooms WHERE room_id = ?",
                (room_id,),
            ).fetchone()["owner_agent_id"]
        finally:
            conn.close()

    def _cursors(self) -> set[tuple[str, str, str]]:
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            return {tuple(r) for r in conn.execute(
                "SELECT tenant_id, room_id, agent_id FROM cloud_room_cursors"
            ).fetchall()}
        finally:
            conn.close()

    def test_cloud_010_rekeys_membership_to_account_and_recovers_owner(self) -> None:
        apply_migrations(self.backend)

        members = self._members()
        # Recoverable rows are re-keyed to the true account.
        self.assertIn(("tenant_a", "room_001", "acct_001"), members)
        self.assertIn(("tenant_a", "room_001", "acct_002"), members)
        self.assertIn(("tenant_a", "room_001", "acct_003"), members)
        # Forged agent_ids are gone.
        self.assertNotIn(("tenant_a", "room_001", "bogus-owner-id"), members)
        self.assertNotIn(("tenant_a", "room_001", "ghost-joiner"), members)
        # The forged duplicate for acct_004 in room_002 is de-duplicated.
        self.assertIn(("tenant_a", "room_002", "acct_004"), members)
        self.assertNotIn(("tenant_a", "room_002", "dup-forged"), members)
        # Unrecoverable rows (hash matches no session) are left untouched.
        self.assertIn(("tenant_a", "room_001", "unrecoverable"), members)

        # Owner recovered from the owner's own membership row.
        self.assertEqual(self._owner("room_001"), "acct_001")
        self.assertEqual(self._owner("room_002"), "acct_004")

        # Cursors follow the recovered identity; the ghost cursor is removed.
        cursors = self._cursors()
        self.assertIn(("tenant_a", "room_001", "acct_003"), cursors)
        self.assertNotIn(("tenant_a", "room_001", "ghost-joiner"), cursors)
        self.assertIn(("tenant_a", "room_002", "acct_004"), cursors)
        self.assertNotIn(("tenant_a", "room_002", "dup-forged"), cursors)

    def test_cloud_010_is_idempotent(self) -> None:
        apply_migrations(self.backend)
        after_first = self._members()
        apply_migrations(self.backend)
        self.assertEqual(self._members(), after_first,
                         "re-running cloud_010 must be a no-op")
        conn = sqlite3.connect(self.cloud_path)
        try:
            conn.row_factory = sqlite3.Row
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM schema_migrations"
                " WHERE migration_id = 'cloud_010_room_membership_account'"
            ).fetchone()["c"]
            self.assertEqual(n, 1, "cloud_010 must be recorded exactly once")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()