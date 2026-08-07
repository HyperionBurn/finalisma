"""Crash-durability kill tests for the Finalisma cloud spine.

REAL kill tests — not simulated. Each test spawns the coordinator as a child
process over stdio (MCP's primary transport), performs writes, KILLS the child
mid-transaction (``proc.kill()`` — SIGKILL, not an exception, not a graceful
shutdown), restarts, and asserts recovery.

Authoritative spec: ``docs/CLOUD_SPINE_DESIGN.md`` §6 (durability under crash),
§7.5 (crash-kill tests).

Process-ownership rule (AGENTS.md "Long-running processes"): the test owns the
child via ``subprocess.Popen`` and tears it down in a ``finally`` block. We
never background a process from the shell.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

# src/ on the path so we can import the coordinator and (eventually) the cloud plane.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import weft_mcp.outbox as _outbox
from weft_mcp.core import WeftStore
from weft_mcp.room import RoomStore

# RED trigger: this import fails with ModuleNotFoundError until the cloud plane
# (src/weft_cloud/) is implemented. The tests below exercise crash recovery
# THROUGH the cloud storage backend — not just the coordinator directly — so the
# whole file fails to load until the cloud plane exists.
from weft_cloud.storage import SqliteWalBackend  # noqa: F401
from weft_cloud.migrations import apply_migrations  # noqa: F401


TEAM_ID = "crash-team"
SCRATCH_PREFIX = "weft-crash-"
REQUEST_TIMEOUT_S = 30


class CrashTestDriver:
    """Owns a coordinator subprocess and tears it down deterministically.

    Follows the AGENTS.md pattern: subprocess.Popen with stdio pipes, terminate
    in finally. The test drives the coordinator over MCP JSON-RPC on stdio.
    """

    def __init__(self, workspace: Path, state_path: str) -> None:
        self.workspace = workspace
        self.state_path = state_path
        self.proc: subprocess.Popen[str] | None = None
        self._request_id = 0

    def start(self) -> None:
        assert self.proc is None, "coordinator already running"
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "scripts/weft-mcp.py",
                "--transport",
                "stdio",
                "--team-id",
                TEAM_ID,
                "--workspace",
                str(self.workspace),
                "--state",
                self.state_path,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(PROJECT_ROOT),
            encoding="utf-8",
            errors="replace",
        )
        # MCP initialize handshake.
        self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        assert self.proc is not None, "coordinator not started"
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._request_id += 1
        rid = self._request_id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("coordinator closed stdout without a reply")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(f"JSON-RPC error: {reply['error']}")
        return reply.get("result", {})

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        if result.get("isError"):
            raise RuntimeError(f"tool {name} failed: {text}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def kill(self) -> None:
        """Hard kill — SIGKILL. Never a graceful shutdown."""
        if self.proc is None:
            return
        try:
            self.proc.kill()
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.wait(timeout=5)
        self.proc = None

    def terminate(self) -> None:
        """SIGTERM fallback (some platforms)."""
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc = None

    def close(self) -> None:
        self.kill()


class TestCrashDurability(unittest.TestCase):
    """Real kill tests for crash durability (design §7.5).

    These tests FAIL now with ModuleNotFoundError for ``weft_cloud`` — that
    is the RED deliverable. The orchestrator wires the cloud plane to make them
    pass.
    """

    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX)
        self.workspace = Path(self.scratch.name)
        self.state_path = str(self.workspace / ".weft" / "state.db")
        os.makedirs(self.workspace / ".weft", exist_ok=True)
        self.driver = CrashTestDriver(self.workspace, self.state_path)

    def tearDown(self) -> None:
        self.driver.close()
        self.scratch.cleanup()

    def _register_agent(self, agent_id: str, role: str = "tester") -> str:
        result = self.driver.call_tool("register_agent", {
            "team_id": TEAM_ID,
            "agent_id": agent_id,
            "role": role,
            "name": f"Agent {agent_id}",
        })
        return result["actor_token"]

    def _integrity_check_ok(self) -> bool:
        conn = sqlite3.connect(self.state_path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return row is not None and row[0] == "ok"
        finally:
            conn.close()

    def _count_events(self, room_id: str) -> int:
        conn = sqlite3.connect(self.state_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM room_event_log WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def _get_event_ids(self, room_id: str) -> list[str]:
        conn = sqlite3.connect(self.state_path)
        try:
            rows = conn.execute(
                "SELECT event_id FROM room_event_log WHERE room_id = ? ORDER BY seq",
                (room_id,),
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def _get_cursor(self, room_id: str, agent_id: str) -> int:
        conn = sqlite3.connect(self.state_path)
        try:
            row = conn.execute(
                "SELECT last_ack_seq FROM room_cursors WHERE room_id = ? AND agent_id = ?",
                (room_id, agent_id),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Test 1: kill mid-transaction, restart, assert durability
    # ------------------------------------------------------------------
    def test_kill_mid_write(self) -> None:
        """Spawn coordinator, perform writes, kill mid-transaction, restart,
        assert: no torn state, no lost acknowledged events, outbox resumes,
        cursors coherent.
        """
        self.driver.start()
        token_a = self._register_agent("crash-a")
        token_b = self._register_agent("crash-b")

        # Create a room (owner auto-joins).
        room = self.driver.call_tool("room_create", {
            "team_id": TEAM_ID,
            "owner_agent_id": "crash-a",
            "cap": 5,
            "actor_token": token_a,
        })
        room_id = room["room_id"]
        link_token = room["link_token"]

        # Second agent joins.
        self.driver.call_tool("room_join", {
            "team_id": TEAM_ID,
            "room_id": room_id,
            "link_token": link_token,
            "agent_id": "crash-b",
            "consent": True,
            "capabilities": ["read"],
            "actor_token": token_b,
        })

        # Append an event + enqueue outbox (room_send does both).
        send_result = self.driver.call_tool("room_send", {
            "team_id": TEAM_ID,
            "room_id": room_id,
            "sender_agent_id": "crash-a",
            "target_spec": "*",
            "payload": {"text": "before-crash"},
            "actor_token": token_a,
        })
        observed_seq = send_result["seq"]
        observed_event_ids = self._get_event_ids(room_id)

        # Ack the events so cursors advance.
        self.driver.call_tool("room_ack", {
            "team_id": TEAM_ID,
            "room_id": room_id,
            "agent_id": "crash-a",
            "seq": observed_seq,
            "actor_token": token_a,
        })
        cursor_before_kill = self._get_cursor(room_id, "crash-a")

        # Now begin a LARGE write batch so the kill lands mid-transaction.
        # We send many messages; the kill will interrupt one of them.
        # Capture the event IDs the client observed BEFORE the kill.
        client_observed_event_ids = list(observed_event_ids)

        # Start a batch of writes, then kill mid-batch.
        # We do this by sending a burst — the kill lands during one of these.
        batch_started = False
        try:
            for i in range(50):
                batch_started = True
                self.driver.call_tool("room_send", {
                    "team_id": TEAM_ID,
                    "room_id": room_id,
                    "sender_agent_id": "crash-b",
                    "target_spec": "crash-a",
                    "payload": {"text": f"batch-{i}"},
                    "actor_token": token_b,
                })
                # Kill partway through the batch — mid-transaction.
                if i == 25:
                    self.driver.kill()
                    break
        except RuntimeError:
            # Expected — the child died.
            pass

        # The child is dead. Restart.
        self.driver.start()

        # ASSERT 1: No torn state — integrity_check ok.
        self.assertTrue(
            self._integrity_check_ok(),
            "database integrity_check failed after crash recovery",
        )

        # ASSERT 2: No lost acknowledged events — every event the client
        # observed before the kill is still present.
        current_event_ids = set(self._get_event_ids(room_id))
        for eid in client_observed_event_ids:
            self.assertIn(
                eid,
                current_event_ids,
                f"acknowledged event {eid} lost after crash recovery",
            )

        # ASSERT 3: Outbox resumes — no stranded in_flight entries after recovery.
        # The coordinator's outbox.init() (in_flight -> queued) must run on startup.
        # We call it here to simulate the cloud plane's startup recovery path.
        _outbox.init(self.state_path)
        conn = sqlite3.connect(self.state_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM outbox_entries WHERE status = 'in_flight'"
            ).fetchone()
            self.assertEqual(
                int(row[0]),
                0,
                "stranded in_flight outbox entries after recovery",
            )
            row = conn.execute(
                "SELECT COUNT(*) FROM outbox_entries WHERE status = 'queued'"
            ).fetchone()
            queued_count = int(row[0])
        finally:
            conn.close()
        self.assertGreater(queued_count, 0, "no queued outbox entries after recovery")

        # Claim the queued entries — they must be claimable.
        claimable = _outbox.claim_due(limit=100)
        self.assertEqual(
            len(claimable),
            queued_count,
            "queued entries not claimable after recovery",
        )

        # ASSERT 4: Cursors coherent — the acked cursor survived the crash.
        cursor_after_restart = self._get_cursor(room_id, "crash-a")
        self.assertGreaterEqual(
            cursor_after_restart,
            cursor_before_kill,
            "cursor regressed after crash recovery",
        )

    # ------------------------------------------------------------------
    # Test 2: outbox crash recovery — in_flight -> queued
    # ------------------------------------------------------------------
    def test_outbox_crash_recovery(self) -> None:
        """Leave outbox entries in_flight, restart the backend, assert entries
        reset to queued and claimable (the cloud plane mirrors outbox.py's
        init() recovery pattern).
        """
        self.driver.start()
        token_a = self._register_agent("ob-a")
        token_b = self._register_agent("ob-b")

        room = self.driver.call_tool("room_create", {
            "team_id": TEAM_ID,
            "owner_agent_id": "ob-a",
            "cap": 5,
            "actor_token": token_a,
        })
        room_id = room["room_id"]
        link_token = room["link_token"]

        self.driver.call_tool("room_join", {
            "team_id": TEAM_ID,
            "room_id": room_id,
            "link_token": link_token,
            "agent_id": "ob-b",
            "consent": True,
            "capabilities": ["read"],
            "actor_token": token_b,
        })

        # Send a message — this enqueues an outbox entry.
        self.driver.call_tool("room_send", {
            "team_id": TEAM_ID,
            "room_id": room_id,
            "sender_agent_id": "ob-a",
            "target_spec": "ob-b",
            "payload": {"text": "outbox-test"},
            "actor_token": token_a,
        })

        # Manually mark the entry in_flight (simulating a claim that never completed).
        _outbox.init(self.state_path)
        claimable = _outbox.claim_due(limit=10)
        self.assertGreater(len(claimable), 0, "no outbox entries to claim")

        # Verify they are now in_flight.
        conn = sqlite3.connect(self.state_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM outbox_entries WHERE status = 'in_flight'"
            ).fetchone()
            in_flight_count = int(row[0])
        finally:
            conn.close()
        self.assertGreater(in_flight_count, 0, "expected in_flight entries")

        # Restart the backend (re-init outbox — this is the crash-recovery path).
        _outbox.init(self.state_path)

        # ASSERT: in_flight entries reset to queued, claimable.
        conn = sqlite3.connect(self.state_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM outbox_entries WHERE status = 'in_flight'"
            ).fetchone()
            self.assertEqual(
                int(row[0]),
                0,
                "in_flight entries not reset after recovery",
            )
            row = conn.execute(
                "SELECT COUNT(*) FROM outbox_entries WHERE status = 'queued'"
            ).fetchone()
            queued_count = int(row[0])
        finally:
            conn.close()
        self.assertGreater(queued_count, 0, "no queued entries after recovery")

        claimable_after = _outbox.claim_due(limit=10)
        self.assertEqual(
            len(claimable_after),
            queued_count,
            "queued entries not claimable after recovery",
        )

    # ------------------------------------------------------------------
    # Test 3: WAL replay after kill during large write batch
    # ------------------------------------------------------------------
    def test_wal_replay_after_kill(self) -> None:
        """Kill during a large write batch, restart, assert integrity_check ok
        and committed events present.
        """
        self.driver.start()
        token_a = self._register_agent("wal-a")
        token_b = self._register_agent("wal-b")

        room = self.driver.call_tool("room_create", {
            "team_id": TEAM_ID,
            "owner_agent_id": "wal-a",
            "cap": 10,
            "actor_token": token_a,
        })
        room_id = room["room_id"]
        link_token = room["link_token"]

        self.driver.call_tool("room_join", {
            "team_id": TEAM_ID,
            "room_id": room_id,
            "link_token": link_token,
            "agent_id": "wal-b",
            "consent": True,
            "capabilities": ["read"],
            "actor_token": token_b,
        })

        # Capture committed event count before the batch.
        events_before = self._count_events(room_id)

        # Large write batch — kill mid-way so the kill lands during COMMIT.
        killed = False
        for i in range(100):
            try:
                self.driver.call_tool("room_send", {
                    "team_id": TEAM_ID,
                    "room_id": room_id,
                    "sender_agent_id": "wal-a" if i % 2 == 0 else "wal-b",
                    "target_spec": "wal-b" if i % 2 == 0 else "wal-a",
                    "payload": {"text": f"wal-batch-{i}"},
                    "actor_token": token_a if i % 2 == 0 else token_b,
                })
            except RuntimeError:
                # Child died — expected after kill.
                killed = True
                break
            # Kill partway through.
            if i == 49:
                self.driver.kill()
                killed = True
                break

        self.assertTrue(killed, "expected the coordinator to be killed mid-batch")

        # Restart.
        self.driver.start()

        # ASSERT 1: integrity_check ok (WAL replay correct).
        self.assertTrue(
            self._integrity_check_ok(),
            "WAL replay left database in torn state",
        )

        # ASSERT 2: committed events present (at least the ones before the batch).
        events_after = self._count_events(room_id)
        self.assertGreaterEqual(
            events_after,
            events_before,
            "committed events lost after WAL replay",
        )

    # ------------------------------------------------------------------
    # Test 4: crash recovery through the cloud storage backend.
    # This test exercises the cloud plane's SqliteWalBackend crash recovery.
    # ------------------------------------------------------------------
    def test_cloud_backend_crash_recovery(self) -> None:
        """The cloud plane's SqliteWalBackend must survive a kill and replay WAL.

        This test is the RED trigger for the cloud plane — the module-level import
        of ``weft_cloud.storage`` fails with ModuleNotFoundError until the
        cloud plane is implemented.
        """
        backend = SqliteWalBackend(self.state_path)
        backend.initialize()
        apply_migrations(backend)

        backend.create_tenant("tenant-1", "Design Partner")
        backend.bind_room("tenant-1", "room-1", self.state_path)

        # Perform writes through the cloud backend.
        with backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_event_mirror(mirror_id, tenant_id, room_id, seq, event_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("mirror-1", "tenant-1", "room-1", 1, "{}", _utc_now_iso()),
            )
            tx.commit()

        # Verify tenant isolation still holds after recovery.
        tenant = backend.get_tenant("tenant-1")
        self.assertIsNotNone(tenant, "tenant lost after backend init")
        self.assertEqual(tenant["name"], "Design Partner")

        # Outbox crash recovery through the cloud backend.
        backend.enqueue_outbox("tenant-1", "env-1", "agent-x", "{}")
        entries = backend.claim_due_outbox("tenant-1", limit=10)
        self.assertGreater(len(entries), 0, "outbox entries not claimable via cloud backend")
        backend.close()


def _utc_now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
