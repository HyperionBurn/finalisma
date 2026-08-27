#!/usr/bin/env python3
"""WAL-safe SQLite backup with rotation, for the production cloud.db.

Production is a single SQLite file (`/var/lib/finalisma/cloud.db` at time of
writing) in WAL mode, serving live traffic while a backup runs. Never `cp` (or
`shutil.copy`) a live WAL database: the main file, `-wal`, and `-shm`
sidecars can be copied at inconsistent points relative to each other, which
can hand you a backup that looks fine and is silently corrupt.

This uses `sqlite3.Connection.backup()`, the API built for exactly this: it
takes an online, page-consistent snapshot of a live database into a fresh
destination file while the source keeps serving writes. A paired
`.manifest.json` sidecar records exact user-table identities, schema
fingerprints, source-time counts, and stable hashes for the rows.

Never world-readable: every backup file is chmod'd 0600 immediately after
creation (best-effort on platforms without POSIX permission bits, e.g.
Windows, where this is a no-op). Never prints credentials or account
identifiers — this module never reads row content, only the destination path
and rotation bookkeeping.

Usage::

    python3 backup_cloud_db.py --src /var/lib/finalisma/cloud.db \\
        --backup-dir /var/backups/weft --keep 14

Writes `<backup-dir>/cloud-<UTC-timestamp>.db` plus its
`cloud-<UTC-timestamp>.db.manifest.json`, deletes older backup pairs beyond
`--keep`, and prints the database path it wrote. Exits non-zero and prints to stderr
on any failure (missing source, destination not writable, backup API error).

An untested backup is not a backup — pair this with restore_drill.py, which
proves a backup this script wrote is actually restorable.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


MANIFEST_FORMAT = "weft-backup-manifest-v1"
MANIFEST_SUFFIX = ".manifest.json"


def manifest_path(backup_path: Path) -> Path:
    """Return the manifest sidecar path paired with ``backup_path``."""
    return backup_path.with_name(backup_path.name + MANIFEST_SUFFIX)


def _quote_identifier(name: str) -> str:
    """Quote a SQLite identifier obtained from sqlite_master safely."""
    return '"' + name.replace('"', '""') + '"'


def _canonical_value(value: object) -> dict[str, str]:
    """Represent one SQLite value without Python hash randomization."""
    if value is None:
        return {"type": "null", "value": ""}
    if isinstance(value, bytes):
        return {
            "type": "blob",
            "value": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        if math.isnan(value):
            rendered = "nan"
        elif math.isinf(value):
            rendered = "inf" if value > 0 else "-inf"
        else:
            rendered = repr(value)
        return {"type": "real", "value": rendered}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    return {"type": type(value).__name__, "value": repr(value)}


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _aggregate_hash(digests: list[str]) -> str:
    """Hash sorted per-row digests so insertion order cannot hide drift."""
    digest = hashlib.sha256()
    for row_digest in sorted(digests):
        digest.update(row_digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _table_schema(
    conn: sqlite3.Connection,
    table_name: str,
    table_sql: str | None,
) -> dict[str, object]:
    quoted = _quote_identifier(table_name)
    columns = []
    for row in conn.execute(f"PRAGMA table_info({quoted})"):
        columns.append({
            "cid": int(row[0]),
            "name": row[1],
            "type": row[2],
            "notnull": int(row[3]),
            "default": row[4],
            "pk": int(row[5]),
        })

    indexes = []
    for row in conn.execute(f"PRAGMA index_list({quoted})"):
        index_name = row[1]
        index_columns = [
            {"seq": int(info[0]), "cid": int(info[1]), "name": info[2]}
            for info in conn.execute(
                f"PRAGMA index_info({_quote_identifier(index_name)})"
            )
        ]
        index_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        indexes.append({
            "name": index_name,
            "unique": int(row[2]),
            "origin": row[3],
            "partial": int(row[4]),
            "columns": index_columns,
            "sql": index_sql_row[0] if index_sql_row else None,
        })
    indexes.sort(key=lambda item: str(item["name"]))

    foreign_keys = [
        {
            "id": int(row[0]),
            "seq": int(row[1]),
            "table": row[2],
            "from": row[3],
            "to": row[4],
            "on_update": row[5],
            "on_delete": row[6],
            "match": row[7],
        }
        for row in conn.execute(f"PRAGMA foreign_key_list({quoted})")
    ]
    foreign_keys.sort(
        key=lambda item: (int(item["id"]), int(item["seq"])),
    )
    triggers = [
        {"name": row[0], "sql": row[1]}
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = ? ORDER BY name",
            (table_name,),
        )
    ]
    return {
        "name": table_name,
        "sql": table_sql,
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "triggers": triggers,
    }


def _table_manifest(
    conn: sqlite3.Connection,
    table_name: str,
    table_sql: str | None,
) -> dict[str, object]:
    schema = _table_schema(conn, table_name, table_sql)
    schema_columns = schema["columns"]
    assert isinstance(schema_columns, list)
    column_names = [str(column["name"]) for column in schema_columns]
    primary_key_columns = [
        str(column["name"])
        for column in sorted(schema_columns, key=lambda column: int(column["pk"]))
        if int(column["pk"]) > 0
    ]
    identity_columns = primary_key_columns or column_names
    identity_indexes = [column_names.index(name) for name in identity_columns]
    row_identity_digests: list[str] = []
    row_digests: list[str] = []
    quoted = _quote_identifier(table_name)
    row_count = 0
    for row in conn.execute(f"SELECT * FROM {quoted}"):
        row_count += 1
        canonical_row = [_canonical_value(value) for value in row]
        canonical_identity = [canonical_row[index] for index in identity_indexes]
        row_identity_digests.append(_sha256_bytes(_canonical_json(canonical_identity)))
        row_digests.append(_sha256_bytes(_canonical_json(canonical_row)))

    return {
        "name": table_name,
        "schema_sha256": _sha256_bytes(_canonical_json(schema)),
        "row_identity_columns": identity_columns,
        "row_identity_sha256": _aggregate_hash(row_identity_digests),
        "rows_sha256": _aggregate_hash(row_digests),
        "row_count": row_count,
    }


def build_manifest(
    database_path: Path,
    *,
    backup_file: str | None = None,
    captured_at: str | None = None,
) -> dict[str, object]:
    """Describe exact user tables and source-time contents without row data."""
    uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        table_manifests = [_table_manifest(conn, name, sql) for name, sql in tables]
    finally:
        conn.close()
    return {
        "format": MANIFEST_FORMAT,
        "backup_file": backup_file or database_path.name,
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
        "tables": table_manifests,
    }


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    """Atomically write a private manifest sidecar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(manifest, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _restrict_permissions(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def backup_database(
    src_path: Path,
    backup_dir: Path,
    *,
    keep: int = 14,
    now: "datetime | None" = None,
) -> Path:
    """Take a WAL-safe online backup of `src_path` into `backup_dir`.

    Returns the path of the newly written database file; its paired manifest
    sidecar is written before this function returns. Raises FileNotFoundError
    if `src_path` does not exist, and sqlite3.Error if the backup API itself
    fails (e.g. source is not a valid SQLite database).
    """
    if not src_path.is_file():
        raise FileNotFoundError(f"source database not found: {src_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    dest_path = backup_dir / f"cloud-{stamp}.db"
    sidecar_path = manifest_path(dest_path)
    if dest_path.exists() or sidecar_path.exists():
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

    try:
        _restrict_permissions(dest_path)
        _write_manifest(
            sidecar_path,
            build_manifest(
                dest_path,
                backup_file=dest_path.name,
            ),
        )
    except Exception:
        # A database without its source-time manifest must never look like a
        # successful backup. Remove the incomplete pair before propagating the
        # original failure to the caller.
        dest_path.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)
        raise

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
        manifest_path(path).unlink(missing_ok=True)
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
    except (FileNotFoundError, FileExistsError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        return 1

    size = dest.stat().st_size
    print(f"backup written: {dest} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
