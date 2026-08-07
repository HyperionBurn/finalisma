from __future__ import annotations

import gc
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import FinalismaStore


class _TraceStore(FinalismaStore):
    def __init__(self, *args, **kwargs):
        self.statements: list[str] = []
        super().__init__(*args, **kwargs)

    def _connect(self, *, query_only: bool = False) -> sqlite3.Connection:
        connection = super()._connect(query_only=query_only)
        connection.set_trace_callback(self.statements.append)
        return connection


class _ConnectionCountStore(FinalismaStore):
    def __init__(self, *args, **kwargs):
        self.connections_created = 0
        super().__init__(*args, **kwargs)

    def _connect(self, *, query_only: bool = False) -> sqlite3.Connection:
        self.connections_created += 1
        return super()._connect(query_only=query_only)


class PerformanceHotPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="finalisma-performance-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        gc.collect()
        self.temporary.cleanup()

    def test_wal_is_initialized_once_and_connection_safety_pragmas_remain(self) -> None:
        statements: list[str] = []
        real_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch("weft_mcp.core.sqlite3.connect", side_effect=traced_connect):
            store = FinalismaStore(self.root / "state.db", self.root / "workspace")
            initialization = list(statements)
            statements.clear()
            with store._read() as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 15_000)
            with store._transaction() as connection:
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 15_000)

        initialization_wal = [sql for sql in initialization if "PRAGMA journal_mode" in sql]
        subsequent_wal_sets = [sql for sql in statements if "PRAGMA journal_mode =" in sql]
        self.assertEqual(len(initialization_wal), 1)
        self.assertEqual(subsequent_wal_sets, [])

    def test_read_context_enforces_query_only_mode(self) -> None:
        store = FinalismaStore(self.root / "state.db", self.root / "workspace")
        with store._read() as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden_write(value TEXT)")

    def test_store_reuses_idle_connections_and_closes_explicitly(self) -> None:
        store = _ConnectionCountStore(self.root / "state.db", self.root / "workspace")
        store.register_agent("demo", "agent-a", role="planner")
        for index in range(20):
            store.route_task("demo", f"Plan work {index}", "Create an execution plan")
        store.health_status()
        self.assertEqual(store.connections_created, 2)

        checked_out = [store._acquire_connection() for _ in range(6)]
        for connection in checked_out:
            store._release_connection(connection)
        self.assertEqual(store._connection_pools[False].qsize(), 4)

        store.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            store.health_status()

    def test_route_load_scoring_uses_one_set_query(self) -> None:
        store = _TraceStore(self.root / "state.db", self.root / "workspace")
        for index in range(12):
            store.register_agent(
                "demo",
                f"agent-{index:02d}",
                role="coder",
                capabilities=["python", "testing"],
            )

        store.statements.clear()
        result = store.route_task("demo", "Implement the Python tests", "Write code and verify it")
        self.assertIsNotNone(result["selection"])
        load_queries = [
            sql
            for sql in store.statements
            if sql.lstrip().upper().startswith("SELECT")
            and "FROM tasks" in sql
            and "claimed_by" in sql
            and "assigned" in sql
        ]
        self.assertLessEqual(len(load_queries), 1)

    def test_authenticated_authorization_uses_one_joined_lookup(self) -> None:
        store = _TraceStore(
            self.root / "state.db",
            self.root / "workspace",
            require_actor_auth=True,
        )
        registered = store.register_agent("demo", "agent-a", role="planner")

        store.statements.clear()
        with store._read() as connection:
            authorized = store._authorize_actor(
                connection,
                "demo",
                "agent-a",
                registered["actor_token"],
            )
        self.assertEqual(authorized["agent_id"], "agent-a")
        authorization_queries = [
            sql
            for sql in store.statements
            if sql.lstrip().upper().startswith("SELECT")
            and ("FROM agents" in sql or "FROM agent_credentials" in sql)
        ]
        self.assertEqual(len(authorization_queries), 1)
        self.assertIn("JOIN agent_credentials", authorization_queries[0])

    def test_session_send_does_not_requery_inserted_event(self) -> None:
        store = _TraceStore(self.root / "state.db", self.root / "workspace")
        store.register_agent("demo", "agent-a", role="planner", capabilities=["planning"])
        pairing = store.create_pairing("agent-a", "demo")
        store.join_pairing(pairing["join_token"], "agent-b", consent=True)

        store.statements.clear()
        result = store.session_send(
            pairing["initiator_session_token"],
            "agent-a",
            "task.delta",
            {"status": "ready"},
            "performance-event-0001",
        )
        self.assertTrue(result["sent"])
        event_rereads = [
            sql
            for sql in store.statements
            if "SELECT * FROM session_events WHERE event_id" in sql
        ]
        self.assertEqual(event_rereads, [])
        idempotency_prereads = [
            sql
            for sql in store.statements
            if "SELECT * FROM session_events WHERE session_id" in sql
            and "idempotency_key" in sql
        ]
        self.assertEqual(idempotency_prereads, [])

    def test_session_poll_and_ack_reuse_joined_cursor_data(self) -> None:
        store = _TraceStore(self.root / "state.db", self.root / "workspace")
        store.register_agent("demo", "agent-a", role="planner")
        pairing = store.create_pairing("agent-a", "demo")
        joined = store.join_pairing(pairing["join_token"], "agent-b", consent=True)
        store.session_send(
            pairing["initiator_session_token"],
            "agent-a",
            "task.delta",
            {"status": "ready"},
            "performance-event-0002",
        )

        store.statements.clear()
        polled = store.session_poll(joined["session_token"], "agent-b", after_seq=0)
        standalone_poll_cursor_reads = [
            sql
            for sql in store.statements
            if sql.lstrip().upper().startswith("SELECT") and "FROM session_cursors" in sql
        ]
        self.assertEqual(standalone_poll_cursor_reads, [])

        store.statements.clear()
        acknowledged = store.session_ack(
            joined["session_token"],
            "agent-b",
            polled["next_seq"],
        )
        self.assertEqual(acknowledged["last_ack_seq"], polled["next_seq"])
        standalone_ack_cursor_reads = [
            sql
            for sql in store.statements
            if sql.lstrip().upper().startswith("SELECT") and "FROM session_cursors" in sql
        ]
        self.assertEqual(standalone_ack_cursor_reads, [])

    def test_task_writes_return_rows_without_post_write_requeries(self) -> None:
        store = _TraceStore(self.root / "state.db", self.root / "workspace")
        for agent_id in ("agent-a", "agent-b", "agent-c"):
            store.register_agent("demo", agent_id, role="worker")

        store.statements.clear()
        created = store.create_task(
            "demo",
            "agent-a",
            "Create a deterministic artifact",
            "Exercise task write return paths.",
            scope=["artifact.txt"],
            preferred_agent="agent-b",
            idempotency_key="performance-task-0001",
        )
        claimed = store.claim_task("demo", "agent-b", created["task"]["task_id"])
        store.update_task(
            "demo",
            "agent-b",
            created["task"]["task_id"],
            progress=80,
            fencing_token=claimed["fencing_token"],
        )
        (self.root / "workspace" / "artifact.txt").write_text("verified\n", encoding="utf-8")
        store.verify_task(
            "demo",
            "agent-b",
            created["task"]["task_id"],
            claimed["fencing_token"],
            ["artifact.txt"],
            [{"name": "fixture", "status": "passed"}],
            reviewer_id="agent-c",
            require_review=True,
        )
        store.complete_task(
            "demo",
            "agent-b",
            created["task"]["task_id"],
            claimed["fencing_token"],
        )
        post_write_requeries = [
            sql
            for sql in store.statements
            if sql.lstrip().upper().startswith("SELECT * FROM TASKS WHERE TASK_ID")
        ]
        self.assertEqual(post_write_requeries, [])

    def test_message_send_and_inbox_reuse_known_row_state(self) -> None:
        store = _TraceStore(self.root / "state.db", self.root / "workspace")
        store.register_agent("demo", "agent-a", role="planner")
        store.register_agent("demo", "agent-b", role="worker")

        store.statements.clear()
        sent = store.send_message(
            "demo",
            "agent-a",
            "task.context",
            {"status": "ready"},
            recipient_id="agent-b",
            idempotency_key="performance-message-0001",
        )
        self.assertTrue(sent["sent"])
        send_requeries = [
            sql
            for sql in store.statements
            if sql.lstrip().upper().startswith("SELECT")
            and ("FROM messages" in sql or "FROM message_reads" in sql)
        ]
        self.assertEqual(send_requeries, [])

        store.statements.clear()
        inbox = store.read_inbox("demo", "agent-b", acknowledge=False)
        self.assertEqual(inbox["count"], 1)
        inbox_read_requeries = [
            sql
            for sql in store.statements
            if sql.lstrip().upper().startswith("SELECT 1 FROM MESSAGE_READS")
        ]
        self.assertEqual(inbox_read_requeries, [])


if __name__ == "__main__":
    unittest.main()
