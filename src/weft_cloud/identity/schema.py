"""Identity — schema bootstrap (migrations wrapper).

The identity plane owns its tables via the cloud migration registry. This
module exposes ``ensure_schema`` so every identity entry point can guarantee
its tables are at the LATEST schema against any ``StorageBackend`` (initialized
or not). It never bails because a single table already exists — an existing
database is always brought up to date — while staying a no-op (no connection
opened) for repeated calls inside the same process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from weft_cloud.migrations import MIGRATIONS, apply_migrations

#: Per-database fingerprint of the applied migration set, keyed by resolved
#: state path. The first ``ensure_schema`` call per database per process runs
#: ``apply_migrations`` and records the fingerprint; later calls are a free
#: no-op that opens no connection. The fingerprint includes the migration SQL,
#: so the cache invalidates automatically if a future migration is added or
#: edited — it can never silently skip a new migration.
_MIGRATION_FINGERPRINT: dict[str, tuple[str, ...]] = {}


def _migration_fingerprint() -> tuple[str, ...]:
    """Tuple identifying the current migration set (ids + every SQL statement)."""
    parts: list[str] = []
    for migration in MIGRATIONS:
        parts.append(migration.migration_id)
        if migration.up_fn is not None:
            parts.append(f"python:{migration.migration_id}")
        elif migration.statements is not None:
            parts.extend(statement.sql for statement in migration.statements)
        else:
            parts.append(migration.up_sql)
    return tuple(parts)


def ensure_schema(backend: Any) -> None:
    """Bring the database fully up to date with every cloud migration.

    Runs the whole migration set on the first call per database per process,
    so an existing database is ALWAYS upgraded to the latest schema on boot —
    including the delivery columns ``cloud_008`` adds to an outbox that
    predates it (previously ``ensure_schema`` returned immediately when
    ``cloud_identity_accounts`` existed and no migration ever ran again). A
    brand-new database is initialized from scratch the same way, and a fully
    up-to-date database is untouched.

    Repeated calls within a process are a zero-cost no-op that opens no
    connection (the fingerprint cache), preserving the hot-path contract the
    identity stores depend on.
    """
    state_path = getattr(backend, "state_path", None)
    if state_path is None:
        # A backend without a stable path cannot be cached; always migrate.
        apply_migrations(backend)
        return
    resolved = str(Path(state_path).expanduser().resolve())
    fingerprint = _migration_fingerprint()
    if _MIGRATION_FINGERPRINT.get(resolved) == fingerprint:
        return
    apply_migrations(backend)
    _MIGRATION_FINGERPRINT[resolved] = fingerprint
