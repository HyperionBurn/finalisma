"""Tests for "backups that are proven, not assumed".

Production is a single SQLite file in WAL mode with real accounts. This
covers:

- scripts/backup_cloud_db.py: a WAL-safe online backup (sqlite3
  Connection.backup(), never a raw file copy) with rotation and restrictive
  permissions.
- scripts/restore_drill.py: "an untested backup is not a backup" — restore
  the newest backup to a temp path, open it, and assert the accounts and
  rooms tables are readable with plausible row counts. Must FAIL LOUDLY,
  never silently, if the restore is unusable.

Stdlib only. Every fixture database lives in a TemporaryDirectory; nothing
here ever touches a real path outside of it, and the drill's "never touches
the original" guarantee is itself asserted with a hash comparison.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from backup_cloud_db import backup_database, newest_backup  # noqa: E402
from restore_drill import DrillFailure, run_restore_drill  # noqa: E402


def _make_fixture_db(path: Path, *, accounts: int = 3, rooms: int = 2, wal: bool = True) -> None:
    """A minimal stand-in for cloud.db's real schema (see
    src/finalisma_cloud/migrations.py: cloud_identity_accounts, cloud_rooms)
    — same table-name shape, without needing the full app schema."""
    conn = sqlite3.connect(str(path))
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE cloud_identity_accounts (account_id TEXT PRIMARY KEY, email TEXT)")
        conn.execute("CREATE TABLE cloud_rooms (room_id TEXT PRIMARY KEY, name TEXT)")
        for i in range(accounts):
            conn.execute("INSERT INTO cloud_identity_accounts VALUES (?, ?)", (f"acc_{i}", f"user{i}@example.com"))
        for i in range(rooms):
            conn.execute("INSERT INTO cloud_rooms VALUES (?, ?)", (f"room_{i}", f"room {i}"))
        conn.commit()
    finally:
        conn.close()


def _ts(offset_seconds: int) -> datetime:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return base.replace(minute=offset_seconds // 60, second=offset_seconds % 60)


class BackupDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "cloud.db"
        _make_fixture_db(self.src)
        self.backup_dir = Path(self.tmp.name) / "backups"

    def test_backup_is_a_faithful_online_snapshot(self):
        dest = backup_database(self.src, self.backup_dir, keep=10)
        self.assertTrue(dest.is_file())
        conn = sqlite3.connect(str(dest))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cloud_identity_accounts").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cloud_rooms").fetchone()[0], 2)
        finally:
            conn.close()

    def test_backup_stays_consistent_with_an_uncommitted_concurrent_write(self):
        # The WAL-unsafe failure mode this replaces is copying the main file
        # without its -wal sidecar mid-write, which can hand you a torn,
        # unreadable file. Hold a write transaction open on a second
        # connection while backing up from a third; the online backup API
        # must still produce a fully consistent, readable snapshot.
        writer = sqlite3.connect(str(self.src))
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN")
        writer.execute("INSERT INTO cloud_identity_accounts VALUES ('acc_uncommitted', 'x@example.com')")
        try:
            dest = backup_database(self.src, self.backup_dir, keep=10)
        finally:
            writer.rollback()
            writer.close()

        backup_conn = sqlite3.connect(str(dest))
        try:
            # Must be readable and at least reflect the committed baseline —
            # what it must NEVER do is raise / be unreadable (a torn copy).
            count = backup_conn.execute("SELECT COUNT(*) FROM cloud_identity_accounts").fetchone()[0]
            self.assertGreaterEqual(count, 3)
            integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok")
        finally:
            backup_conn.close()

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not meaningful on Windows")
    def test_permissions_restricted_never_world_readable(self):
        dest = backup_database(self.src, self.backup_dir, keep=10)
        mode = dest.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"backup must not be world/group readable, got {oct(mode)}")

    def test_rotation_keeps_only_newest_n(self):
        paths = [backup_database(self.src, self.backup_dir, keep=3, now=_ts(i)) for i in range(5)]
        remaining = sorted(self.backup_dir.glob("cloud-*.db"))
        self.assertEqual([p.name for p in remaining], [p.name for p in paths[-3:]])

    def test_rotation_disabled_when_keep_is_zero(self):
        for i in range(4):
            backup_database(self.src, self.backup_dir, keep=0, now=_ts(i))
        remaining = sorted(self.backup_dir.glob("cloud-*.db"))
        self.assertEqual(len(remaining), 4, "keep<=0 must mean 'keep everything', not 'keep none'")

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            backup_database(Path(self.tmp.name) / "does-not-exist.db", self.backup_dir)

    def test_never_prints_credentials_or_email_addresses(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "backup_cloud_db.py"),
             "--src", str(self.src), "--backup-dir", str(self.backup_dir)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("@example.com", result.stdout)

    def test_newest_backup_helper(self):
        self.assertIsNone(newest_backup(self.backup_dir))
        backup_database(self.src, self.backup_dir, keep=10, now=_ts(0))
        second = backup_database(self.src, self.backup_dir, keep=10, now=_ts(1))
        self.assertEqual(newest_backup(self.backup_dir), second)


class RestoreDrillTests(unittest.TestCase):
    """"An untested backup is not a backup." Every one of these fixtures
    represents a way a restore can silently be unusable; each must FAIL
    LOUDLY (a raised DrillFailure with a clear message), never report
    success by omission."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "cloud.db"
        _make_fixture_db(self.src, accounts=258, rooms=12)  # mirrors "~258 real accounts"
        self.backup_dir = Path(self.tmp.name) / "backups"
        self.backup_path = backup_database(self.src, self.backup_dir, keep=10)

    def test_good_backup_passes_with_plausible_counts(self):
        result = run_restore_drill(self.backup_path, min_accounts=1)
        self.assertEqual(result.accounts_count, 258)
        self.assertEqual(result.rooms_count, 12)
        self.assertIn("PASSED", result.summary())

    def test_never_touches_the_original_backup_file(self):
        before = hashlib.sha256(self.backup_path.read_bytes()).hexdigest()
        run_restore_drill(self.backup_path, min_accounts=1)
        after = hashlib.sha256(self.backup_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_empty_accounts_table_fails_loudly(self):
        empty_src = Path(self.tmp.name) / "empty.db"
        _make_fixture_db(empty_src, accounts=0, rooms=0)
        empty_backup = backup_database(empty_src, Path(self.tmp.name) / "empty-backups", keep=10)
        with self.assertRaises(DrillFailure) as ctx:
            run_restore_drill(empty_backup, min_accounts=1)
        self.assertIn("row", str(ctx.exception))

    def test_zero_min_accounts_allows_a_legitimately_empty_db(self):
        empty_src = Path(self.tmp.name) / "empty2.db"
        _make_fixture_db(empty_src, accounts=0, rooms=0)
        empty_backup = backup_database(empty_src, Path(self.tmp.name) / "empty2-backups", keep=10)
        result = run_restore_drill(empty_backup, min_accounts=0)
        self.assertEqual(result.accounts_count, 0)

    def test_missing_backup_file_fails_loudly(self):
        with self.assertRaises(DrillFailure):
            run_restore_drill(Path(self.tmp.name) / "does-not-exist.db")

    def test_zero_byte_backup_fails_loudly(self):
        bogus = Path(self.tmp.name) / "zero.db"
        bogus.write_bytes(b"")
        with self.assertRaises(DrillFailure):
            run_restore_drill(bogus)

    def test_corrupt_file_fails_loudly_not_a_crash(self):
        bogus = Path(self.tmp.name) / "corrupt.db"
        bogus.write_bytes(b"this is not a sqlite database, just garbage bytes" * 100)
        with self.assertRaises(DrillFailure) as ctx:
            run_restore_drill(bogus)
        self.assertTrue(str(ctx.exception))

    def test_missing_tables_fails_loudly(self):
        odd_db = Path(self.tmp.name) / "odd.db"
        conn = sqlite3.connect(str(odd_db))
        conn.execute("CREATE TABLE unrelated_stuff (x INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(DrillFailure) as ctx:
            run_restore_drill(odd_db)
        self.assertIn("no table matching", str(ctx.exception))

    def test_finds_tables_by_substring_if_exact_names_absent(self):
        # Defends against a future schema rename blinding the drill entirely.
        renamed_db = Path(self.tmp.name) / "renamed.db"
        conn = sqlite3.connect(str(renamed_db))
        conn.execute("CREATE TABLE tenant_accounts_v2 (id TEXT)")
        conn.execute("CREATE TABLE tenant_rooms_v2 (id TEXT)")
        conn.execute("INSERT INTO tenant_accounts_v2 VALUES ('a')")
        conn.commit()
        conn.close()
        result = run_restore_drill(renamed_db, min_accounts=1)
        self.assertEqual(result.accounts_table, "tenant_accounts_v2")
        self.assertEqual(result.rooms_table, "tenant_rooms_v2")

    def test_never_prints_row_content_or_email_addresses(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "restore_drill.py"),
             "--backup-dir", str(self.backup_dir), "--min-accounts", "1"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASSED", result.stdout)
        self.assertNotIn("@example.com", result.stdout)

    def test_cli_fails_loudly_with_nonzero_exit_and_clear_message(self):
        empty_src = Path(self.tmp.name) / "empty3.db"
        _make_fixture_db(empty_src, accounts=0, rooms=0)
        empty_dir = Path(self.tmp.name) / "empty3-backups"
        backup_database(empty_src, empty_dir, keep=10)
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "restore_drill.py"),
             "--backup-dir", str(empty_dir), "--min-accounts", "1"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RESTORE DRILL FAILED", result.stderr)

    def test_picks_the_newest_backup_from_a_directory(self):
        # setUp's backup used the real current time; force this one to be
        # unambiguously later regardless of when the test happens to run.
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        newer = backup_database(self.src, self.backup_dir, keep=10, now=far_future)
        cli = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "restore_drill.py"),
             "--backup-dir", str(self.backup_dir), "--min-accounts", "1"],
            capture_output=True, text=True,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertIn(newer.name, cli.stdout)


if __name__ == "__main__":
    unittest.main()
