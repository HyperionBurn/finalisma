"""Identity — session context + role guard (auth analog of TenantContext).

``SessionContext`` is what handlers receive instead of a raw (account_id,
role) pair they could lie about. The role is DERIVED at session-validation
time from the session row that was itself issued from the DB.

Defence in depth (analogous to Wave F tenancy): the service layer checks
``ctx.require_role(...)`` — and the orgs/invites services ALSO re-read the
actor's role from ``cloud_identity_members`` (``require_db_role``) so a caller
that reaches past the service layer still cannot act above its actual
membership. One forgotten guard still leaves the other in place.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 8, 6.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ROLE_RANK = {"member": 0, "admin": 1, "owner": 2}


class RoleError(Exception):
    """Insufficient role for the requested action."""

    def __init__(self, message: str = "forbidden"):
        super().__init__(message)
        self.code = "forbidden"


@dataclass(frozen=True)
class SessionContext:
    """One per authenticated request — the only way handlers reach identity.

    Constructed by ``sessions.validate`` from the DB session row. The role is
    a property of the authenticated session, never an accepted argument.
    """

    tenant_id: str
    account_id: str
    role: str
    backend: Any  # StorageBackend

    def require_role(self, required: str) -> None:
        """Layer-1 guard: refuse if the session's role is below ``required``."""
        if ROLE_RANK[self.role] < ROLE_RANK[required]:
            raise RoleError("forbidden")


def require_db_role(backend: Any, tenant_id: str, account_id: str, required: str) -> None:
    """Layer-2 guard: re-derive the actor's role from cloud_identity_members.

    A SessionContext role can be forged by a caller who constructs one
    directly. This guard reads the membership row — the source of truth — and
    refuses if the DB role is below ``required``. The orgs/invites service
    methods call this in addition to ``ctx.require_role``.
    """
    with backend.transaction() as tx:
        row = tx.execute(
            "SELECT role FROM cloud_identity_members WHERE tenant_id = ? AND account_id = ?",
            (tenant_id, account_id),
        ).fetchone()
    role = row["role"] if row else None
    if role is None or ROLE_RANK.get(role, -1) < ROLE_RANK[required]:
        raise RoleError("forbidden")
