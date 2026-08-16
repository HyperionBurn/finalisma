#!/usr/bin/env python3
"""WAL-safe SQLite backup with rotation, for the production cloud.db.

Production is a single SQLite file (`/var/lib/finalisma/cloud.db` at time of
writing) in WAL mode, serving live traffic while a backup runs. Never `cp` (or
`shutil.copy`) a live WAL database: the main file, `-wal`, and `-shm`
sidecars can be copied at inconsistent points relative to each other, which
can hand you a backup that looks fine and is silently corrupt.

This uses `sqlite3.Connection.backup()`, the API built for exactly this: it
takes an online, page-consistent snapshot of a live database into a fresh
destination file while the source keeps serving writes.

Never world-readable: every backup file is chmod'd 0600 immediately after
creation (best-effort on platforms without POSIX permission bits, e.g.
Windows, where this is a no-op). Never prints credentials or account
identifiers — this module never reads row content, only the destination path
and rotation bookkeeping.

Usage::

    python3 backup_cloud_db.py --src /var/lib/finalisma/cloud.db \\
        --backup-dir /var/backups/weft --keep 14

Writes `<backup-dir>/cloud-<UTC-timestamp>.db`, deletes older backups beyond
`--keep`, and prints the path it wrote. Exits non-zero and prints to stderr
on any failure (missing source, destination not writable, backup API error).

An untested backup is not a backup — pair this with restore_drill.py, which
proves a backup this script wrote is actually restorable.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def backup_database(
    src_path: Path,
    backup_dir: Path,
    *,
    keep: int = 14,
    now: "datetime | None" = None,
) -> Path:
    """Take a WAL-safe online backup of `src_path` into `backup_dir`.

    Returns the path of the newly written backup file. Raises FileNotFoundError
    if `src_path` does not exist, and sqlite3.Error if the backup API itself
    fails (e.g. source is not a valid SQLite database).
    """
    if not src_path.is_file():
        raise FileNotFoundError(f"source database not found: {src_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    dest_path = backup_dir / f"cloud-{stamp}.db"
    if dest_path.exists():
        # Same-second collision (e.g. tests). Never silently overwrite a backup.
        raise FileExistsError(f"backup destination already exists: {dest_path}")

    # as_posix(): sqlite's URI parser wants forward slashes even on Windows;
    # resolve(): a relative src_path must not silently mean something
    # different once we've chdir'd (we never do, but don't depend on it).
    src_uri = f"file:{src_path.resolve().as_posix()}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    try:
        dest_conn = sqlite3.connect(str(dest_path))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()

    _restrict_permissions(dest_path)
    _rotate(backup_dir, keep=keep)
    return dest_path


def _restrict_permissions(path: Path) -> None:
    """chmod 0600. Best-effort: Windows has no POSIX bits to set."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _rotate(backup_dir: Path, *, keep: int) -> "list[Path]":
    """Delete backups beyond the newest `keep`. Returns the deleted paths.

    keep <= 0 means "keep everything" (rotation disabled), not "keep none" —
    a typo should never be able to delete every backup you have.
    """
    if keep <= 0:
        return []
    backups = sorted(backup_dir.glob("cloud-*.db"))
    stale = backups[:-keep] if len(backups) > keep else []
    deleted = []
    for path in stale:
        path.unlink()
        deleted.append(path)
    return deleted


def newest_backup(backup_dir: Path) -> "Path | None":
    """Return the most recent backup file in `backup_dir`, or None if empty."""
    backups = sorted(backup_dir.glob("cloud-*.db"))
    return backups[-1] if backups else None


def _main(argv: "list[str]") -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True, type=Path, help="path to the live cloud.db")
    parser.add_argument("--backup-dir", required=True, type=Path, help="directory to write backups into")
    parser.add_argument("--keep", type=int, default=14, help="how many backups to retain (default 14)")
    args = parser.parse_args(argv)

    try:
        dest = backup_database(args.src, args.backup_dir, keep=args.keep)
    except (FileNotFoundError, FileExistsError, sqlite3.Error) as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        return 1

    size = dest.stat().st_size
    print(f"backup written: {dest} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
