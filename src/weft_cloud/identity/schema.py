"""Identity — schema bootstrap (migrations wrapper).

The identity plane owns its tables via the cloud migration registry. This
module exposes ``ensure_schema`` so every identity entry point can guarantee
its tables exist against any ``StorageBackend`` (initialized or not), while
staying a no-op when they are already present.
"""

from __future__ import annotations

from typing import Any

from weft_cloud.migrations import apply_migrations


def ensure_schema(backend: Any) -> None:
    """Apply pending cloud migrations (including cloud_002..cloud_006).

    Cheap no-op once ``cloud_identity_accounts`` exists, so per-call
    enforcement inside hot identity paths does not reopen connections.
    """
    try:
        with backend.transaction() as tx:
            row = tx.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_identity_accounts'"
            ).fetchone()
    except Exception:
        row = None
    if row is not None:
        return
    apply_migrations(backend)
