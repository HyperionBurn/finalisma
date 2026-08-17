#!/usr/bin/env python3
"""Prove a backup is actually restorable. An untested backup is not a backup.

Restores the newest (or an explicitly named) backup file to a private temp
path, opens the COPY read-only, and asserts the accounts and rooms tables are
present and readable with plausible row counts. This is the drill described
in the ops brief: "restore the newest backup to a temp path, open it, assert
the accounts and rooms tables are readable with plausible row counts. ...
It must FAIL loudly if the restore is unusable."

Safety properties, all enforced and covered by tests/test_deploy_backup.py:

- Never touches the original backup file. It is only ever read (copied out,
  then the copy is opened). A SHA-256 check before/after proves this.
- Never touches production. This script's input is a backup FILE, not the
  live database; it has no code path that opens `--src`-style live paths.
- Never prints row content, credentials, or email addresses — only table
  names and integer counts.
- Fails loudly (non-zero exit, explicit message) on: missing backup,
  zero-byte backup, a file that is not a SQLite database, a database that
  fails `PRAGMA integrity_check`, or missing/unreadable accounts/rooms
  tables. It never reports success by omission.

Usage::

    python3 restore_drill.py --backup-dir /var/backups/weft --min-accounts 1
    python3 restore_drill.py --backup-file /path/to/one/backup.db

Exits 0 and prints a PASS summary if the drill succeeds; exits 1 and prints
a FAIL summary (never silently) otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Prefer the exact production name if present, then this repo's actual
# migrations.py schema name, then fall back to a substring search — the
# drill should not go blind just because a table got renamed.
ACCOUNTS_TABLE_CANDIDATES = ("accounts", "cloud_identity_accounts")
ROOMS_TABLE_CANDIDATES = ("rooms", "cloud_rooms")


class DrillFailure(Exception):
    """Raised for every "the restore is unusable" case. Never caught silently."""


@dataclass(frozen=True)
class DrillResult:
    backup_path: Path
    accounts_table: str
    accounts_count: int
    rooms_table: str
    rooms_count: int

    def summary(self) -> str:
        return (
            f"restore drill PASSED for {self.backup_path.name}: "
            f"{self.accounts_table}={self.accounts_count} row(s), "
            f"{self.rooms_table}={self.rooms_count} row(s)"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_table(conn: sqlite3.Connection, candidates: "tuple[str, ...]") -> str:
    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for name in candidates:
        if name in existing:
            return name
    # Fall back to a substring match so a schema rename doesn't blind the drill.
    needle = candidates[0].rstrip("s")  # "accounts" -> "account", "rooms" -> "room"
    for name in sorted(existing):
        if needle in name.lower():
            return name
    raise DrillFailure(
        f"no table matching {candidates} found. Tables present: {sorted(existing) or '(none)'}"
    )


def run_restore_drill(
    backup_path: Path,
    *,
    min_accounts: int = 0,
    min_rooms: int = 0,
    accounts_candidates: "tuple[str, ...]" = ACCOUNTS_TABLE_CANDIDATES,
    rooms_candidates: "tuple[str, ...]" = ROOMS_TABLE_CANDIDATES,
) -> DrillResult:
    """Restore `backup_path` to a temp copy and verify it. Raises DrillFailure
    (never returns a "success" result) if the backup is unusable."""
    if not backup_path.is_file():
        raise DrillFailure(f"backup file not found: {backup_path}")
    if backup_path.stat().st_size == 0:
        raise DrillFailure(f"backup file is zero bytes: {backup_path}")

    before_hash = _sha256(backup_path)

    with tempfile.TemporaryDirectory(prefix="weft-restore-drill-") as tmp:
        copy_path = Path(tmp) / backup_path.name
        shutil.copy2(backup_path, copy_path)

        try:
            copy_uri = f"file:{copy_path.resolve().as_posix()}?mode=ro"
            conn = sqlite3.connect(copy_uri, uri=True)
        except sqlite3.Error as exc:
            raise DrillFailure(f"could not open restored copy: {exc}") from exc

        try:
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.DatabaseError as exc:
                raise DrillFailure(f"restored copy is not a valid SQLite database: {exc}") from exc
            if not integrity or integrity[0] != "ok":
                raise DrillFailure(f"PRAGMA integrity_check failed: {integrity}")

            accounts_table = _find_table(conn, accounts_candidates)
            rooms_table = _find_table(conn, rooms_candidates)

            try:
                accounts_count = conn.execute(f"SELECT COUNT(*) FROM {accounts_table}").fetchone()[0]
                rooms_count = conn.execute(f"SELECT COUNT(*) FROM {rooms_table}").fetchone()[0]
            except sqlite3.Error as exc:
                raise DrillFailure(f"tables exist but are not readable: {exc}") from exc

            if accounts_count < min_accounts:
                raise DrillFailure(
                    f"{accounts_table} has {accounts_count} row(s), expected at least "
                    f"{min_accounts} — restore looks unusable (empty/truncated)"
                )
            if rooms_count < min_rooms:
                raise DrillFailure(
                    f"{rooms_table} has {rooms_count} row(s), expected at least {min_rooms}"
                )
        finally:
            conn.close()

    after_hash = _sha256(backup_path)
    if before_hash != after_hash:
        # This should be unreachable (we only ever read backup_path), but a
        # drill that might have mutated the one artifact it exists to protect
        # must never report success.
        raise DrillFailure(
            "backup file changed during the drill (hash mismatch) — refusing to report PASS"
        )

    return DrillResult(backup_path, accounts_table, accounts_count, rooms_table, rooms_count)


def _main(argv: "list[str]") -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backup-dir", type=Path, help="pick the newest cloud-*.db in this directory")
    group.add_argument("--backup-file", type=Path, help="drill this exact backup file")
    parser.add_argument("--min-accounts", type=int, default=0, help="fail if fewer rows than this")
    parser.add_argument("--min-rooms", type=int, default=0, help="fail if fewer rows than this")
    args = parser.parse_args(argv)

    if args.backup_file is not None:
        target = args.backup_file
    else:
        # Local import avoids a hard dependency when this file is used standalone.
        from backup_cloud_db import newest_backup

        target = newest_backup(args.backup_dir)
        if target is None:
            print(f"RESTORE DRILL FAILED: no cloud-*.db backups found in {args.backup_dir}", file=sys.stderr)
            return 1

    try:
        result = run_restore_drill(
            target, min_accounts=args.min_accounts, min_rooms=args.min_rooms
        )
    except DrillFailure as exc:
        print(f"RESTORE DRILL FAILED: {exc}", file=sys.stderr)
        return 1

    print(result.summary())
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(_main(sys.argv[1:]))
