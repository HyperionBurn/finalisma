"""Regression coverage for subprocess and temporary-directory teardown."""

from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._process_cleanup import cleanup_tempdir, stop_subprocess


class _TrackedStream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StubbornProcess:
    def __init__(self) -> None:
        self.stdin = _TrackedStream()
        self.stdout = _TrackedStream()
        self.stderr = _TrackedStream()
        self.events: list[str] = []
        self.wait_calls = 0

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.events.append("terminate")

    def kill(self) -> None:
        self.events.append("kill")

    def wait(self, *, timeout: float) -> None:
        self.wait_calls += 1
        self.events.append(f"wait:{timeout:g}")
        raise subprocess.TimeoutExpired("fixture", timeout)


class ProcessCleanupRegressionTests(unittest.TestCase):
    def test_stop_subprocess_bounds_terminate_then_kill_and_closes_streams(self) -> None:
        process = _StubbornProcess()

        with self.assertRaises(subprocess.TimeoutExpired):
            stop_subprocess(process)

        self.assertEqual(process.events, ["terminate", "wait:10", "kill", "wait:10"])
        self.assertEqual(process.wait_calls, 2)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_cleanup_tempdir_releases_repeated_sqlite_directories(self) -> None:
        for _ in range(10):
            temporary = tempfile.TemporaryDirectory(prefix="process-cleanup-")
            try:
                database = Path(temporary.name) / "state.db"
                connection = sqlite3.connect(database)
                try:
                    connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
                    connection.execute("INSERT INTO state VALUES ('closed')")
                    connection.commit()
                finally:
                    connection.close()
                cleanup_tempdir(temporary, attempts=3)
                self.assertFalse(Path(temporary.name).exists())
            finally:
                # cleanup_tempdir is idempotent for the already-removed directory.
                if Path(temporary.name).exists():
                    temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
