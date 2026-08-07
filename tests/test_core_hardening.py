"""Targeted hardening tests for src/weft_mcp/core.py."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftError, WeftStore


class SessionReadNoWriteTest(unittest.TestCase):
    """Verify _require_session_read never writes to the DB.

    BUG: session_poll() and session_status() open a _read() context
    (BEGIN DEFERRED) but the old _require_session() performed an UPDATE
    when a session was expired.  Under WAL concurrent access this caused
    SQLite BUSY on the reader.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WeftStore(self.root / ".weft" / "state.db", self.root, heartbeat_timeout=30)
        self.store.register_agent("t", "a1", "A1", "architect", None, [])
        self.store.register_agent("t", "a2", "A2", "coder", None, [])
        self.pairing = self.store.create_pairing("a1", "t")
        self.joined = self.store.join_pairing(self.pairing["join_token"], "a2", consent=True)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_session_poll_on_expired_session_does_not_write(self) -> None:
        """session_poll must raise session_expired but must NOT flip the
        session state to 'expired' because it runs inside _read()."""
        session_token = self.joined["session_token"]
        session_id = self.joined["session_id"]

        # Force the session to appear expired by mocking _epoch to a far-future time.
        future = 2_000_000_000.0
        with patch("weft_mcp.core._epoch", return_value=future):
            with self.assertRaises(WeftError) as ctx:
                self.store.session_poll(session_token, "a2", after_seq=0)
            self.assertEqual(ctx.exception.code, "session_expired")

        # After the failed poll, the session row must STILL say 'active'
        # because the read path must not have written.
        with self.store._read() as conn:
            row = conn.execute("SELECT state FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "active")

    def test_session_status_on_expired_session_does_not_write(self) -> None:
        """Same invariant for session_status: read-only, no state mutation."""
        session_token = self.joined["session_token"]
        session_id = self.joined["session_id"]

        future = 2_000_000_000.0
        with patch("weft_mcp.core._epoch", return_value=future):
            with self.assertRaises(WeftError) as ctx:
                self.store.session_status(session_token, "a2")
            self.assertEqual(ctx.exception.code, "session_expired")

        with self.store._read() as conn:
            row = conn.execute("SELECT state FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            self.assertEqual(row["state"], "active")

    def test_session_send_on_expired_session_raises_with_rollback(self) -> None:
        """session_send uses _require_session (write context) and raises
        session_expired. The UPDATE is rolled back by _transaction's
        exception handler, so the session row stays 'active'."""
        session_token = self.joined["session_token"]

        future = 2_000_000_000.0
        with patch("weft_mcp.core._epoch", return_value=future):
            with self.assertRaises(WeftError) as ctx:
                self.store.session_send(
                    session_token, "a2", "test.event", {"v": 1}, "send-expire-test"
                )
            self.assertEqual(ctx.exception.code, "session_expired")

    def test_read_safe_variant_raises_correctly_for_closed_session(self) -> None:
        """_require_session_read still rejects closed sessions (no silent pass)."""
        session_token = self.joined["session_token"]
        self.store.close_session(self.pairing["initiator_session_token"], "a1")
        with self.assertRaises(WeftError) as ctx:
            with self.store._read() as conn:
                self.store._require_session_read(conn, session_token, "a2")
            self.assertEqual(ctx.exception.code, "session_closed")


class ConnectionCloseOnTeardownTest(unittest.TestCase):
    """Regression: close() must close in-flight connections too.

    Windows keeps a SQLite file locked while ANY connection to it is open.
    The old close() only drained the idle pool, so a checked-out connection
    (e.g. a thread mid-operation at teardown) left the file locked and
    tempfile.cleanup() intermittently raised PermissionError — an errors=1
    flake that passed on re-run. close() must force-close in-flight
    connections as well.
    """

    def test_close_closes_in_flight_connection_allowing_tempdir_cleanup(self) -> None:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        store = WeftStore(root / "state.db", root)
        # Acquire a connection and deliberately NOT release it — simulates a
        # thread holding a checked-out connection at teardown. Whether it came
        # from the pool or was freshly created, close() must close it.
        conn = store._acquire_connection()
        conn.execute("SELECT 1").fetchone()
        # close() must force-close the in-flight connection.
        store.close()
        # The SQLite file must now be removable on Windows (no open handle).
        state_file = root / "state.db"
        try:
            state_file.unlink()
        except PermissionError as exc:
            self.fail(f"state.db still locked after close(): {exc}")
        temp.cleanup()

    def test_close_is_idempotent_and_clears_live_connections(self) -> None:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        store = WeftStore(root / "state.db", root)
        conn = store._acquire_connection()
        store.close()
        store.close()  # second close is a no-op
        self.assertEqual(store._live_connections, set())
        temp.cleanup()


if __name__ == "__main__":
    unittest.main()
