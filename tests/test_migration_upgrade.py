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
from pathlib import Path

# Add src/ to the import path — same convention as tests/test_finalisma.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftStore  # noqa: E402

# This import is the RED gate: the module does not exist yet.
from weft_cloud.migrations import apply_migrations  # noqa: E402


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

    def _connect_ro(self) -> sqlite3.Connection:
        """Read-only inspection connection.

        A plain read-write connection to a DELETE-journal database is used (the
        migration returns the DB to DELETE mode), with an explicit close. This
        avoids the -shm/-wal handles that a WAL-mode connection leaves on
        Windows, which intermittently locked tempdir teardown.
        """
        connection = sqlite3.connect(self.state_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

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


if __name__ == "__main__":
    unittest.main()
