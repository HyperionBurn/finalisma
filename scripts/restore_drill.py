#!/usr/bin/env python3
"""Prove a backup is actually restorable. An untested backup is not a backup.

Restores the newest (or an explicitly named) backup file to a private temp
path, opens the COPY read-only, and verifies its paired source-time manifest:
exact table identities, schema fingerprints, row identities, row hashes, and
counts. It then asserts the accounts and rooms tables are present and
readable. This is the drill described
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
- Fails loudly (non-zero exit, explicit message) on: missing backup or
  manifest,
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
import json
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from backup_cloud_db import MANIFEST_FORMAT, build_manifest, manifest_path

# Only these exact schema identities are approved for the account and room
# anchors. A fuzzy substring fallback can bless an unrelated table after a
# partial restore, which is precisely the false confidence this drill exists
# to prevent.
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


def _find_approved_table(
    table_names: set[str],
    candidates: "tuple[str, ...]",
    kind: str,
) -> str:
    for name in candidates:
        if name in table_names:
            return name
    raise DrillFailure(
        f"manifest has no approved {kind} table; expected one of {candidates}, "
        f"found {sorted(table_names) or '(none)'}"
    )


def _read_manifest(backup_path: Path) -> dict[str, object]:
    path = manifest_path(backup_path)
    if not path.is_file():
        raise DrillFailure(f"manifest file not found for backup: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DrillFailure(f"manifest is unreadable: {path}") from exc
    if not isinstance(manifest, dict):
        raise DrillFailure("manifest must be a JSON object")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise DrillFailure(
            f"unsupported manifest format: {manifest.get('format')!r}"
        )
    if not isinstance(manifest.get("backup_file"), str) or not manifest["backup_file"]:
        raise DrillFailure("manifest has no backup filename")
    tables = manifest.get("tables")
    if not isinstance(tables, list) or not tables:
        raise DrillFailure("manifest contains no approved table identities")
    names: list[str] = []
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("name"), str):
            raise DrillFailure("manifest contains an invalid table identity")
        name = table["name"]
        if not name or name.lower().startswith("sqlite_") or name in names:
            raise DrillFailure("manifest contains duplicate or reserved table names")
        for field in ("schema_sha256", "row_identity_sha256", "rows_sha256"):
            value = table.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise DrillFailure(f"manifest table {name!r} has invalid {field}")
        identity_columns = table.get("row_identity_columns")
        if not isinstance(identity_columns, list) or not all(
            isinstance(column, str) for column in identity_columns
        ):
            raise DrillFailure(f"manifest table {name!r} has invalid identity columns")
        if (
            type(table.get("row_count")) is not int
            or table["row_count"] < 0
        ):
            raise DrillFailure(f"manifest table {name!r} has invalid row count")
        names.append(name)
    if names != sorted(names):
        raise DrillFailure("manifest table identities are not sorted")
    if not isinstance(manifest.get("captured_at"), str) or not manifest["captured_at"]:
        raise DrillFailure("manifest has no source capture time")
    return manifest


def _manifest_table_map(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    tables = manifest["tables"]
    assert isinstance(tables, list)
    return {table["name"]: table for table in tables if isinstance(table, dict)}


def _manifest_mismatch(
    expected: dict[str, object],
    actual: dict[str, object],
) -> str | None:
    expected_tables = _manifest_table_map(expected)
    actual_tables = _manifest_table_map(actual)
    expected_names = set(expected_tables)
    actual_names = set(actual_tables)
    if expected_names != actual_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        return f"table identities changed (missing={missing}, unexpected={unexpected})"
    for name in sorted(expected_names):
        expected_table = expected_tables[name]
        actual_table = actual_tables[name]
        for field in (
            "schema_sha256",
            "row_identity_columns",
            "row_identity_sha256",
            "rows_sha256",
            "row_count",
        ):
            if actual_table.get(field) != expected_table.get(field):
                return f"manifest mismatch for table {name!r}: {field} changed"
    return None


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

    source_manifest = _read_manifest(backup_path)
    source_manifest_path = manifest_path(backup_path)
    before_hash = _sha256(backup_path)
    manifest_before_hash = _sha256(source_manifest_path)

    with tempfile.TemporaryDirectory(prefix="weft-restore-drill-") as tmp:
        copy_path = Path(tmp) / backup_path.name
        copy_manifest_path = manifest_path(copy_path)
        shutil.copy2(backup_path, copy_path)
        shutil.copy2(source_manifest_path, copy_manifest_path)

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

            try:
                actual_manifest = build_manifest(
                    copy_path,
                    backup_file=backup_path.name,
                    captured_at=source_manifest["captured_at"],
                )
            except sqlite3.DatabaseError as exc:
                raise DrillFailure(
                    f"could not fingerprint restored copy: {exc}"
                ) from exc
            mismatch = _manifest_mismatch(source_manifest, actual_manifest)
            if mismatch is not None:
                raise DrillFailure(f"backup manifest mismatch: {mismatch}")

            actual_tables = _manifest_table_map(actual_manifest)
            table_names = set(actual_tables)
            accounts_table = _find_approved_table(
                table_names, accounts_candidates, "accounts",
            )
            rooms_table = _find_approved_table(
                table_names, rooms_candidates, "rooms",
            )
            accounts_count = int(actual_tables[accounts_table]["row_count"])
            rooms_count = int(actual_tables[rooms_table]["row_count"])

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

    try:
        manifest_after_hash = _sha256(source_manifest_path)
    except OSError as exc:
        raise DrillFailure("manifest disappeared during the drill") from exc
    if manifest_before_hash != manifest_after_hash:
        raise DrillFailure(
            "manifest changed during the drill - refusing to report PASS"
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
