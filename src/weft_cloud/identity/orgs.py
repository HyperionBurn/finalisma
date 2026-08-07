"""Identity — orgs, membership, roles (structural role enforcement).

An org IS a Wave F tenant: ``cloud_tenants`` is the only org table, and every
identity table is scoped by ``tenant_id``. There is no parallel org id.

Role enforcement is DEFENCE IN DEPTH (analogous to Wave F tenancy):
  - Layer 1: ``ctx.require_role(...)`` at the service boundary — the role
    comes from the authenticated session, never from a client argument.
  - Layer 2: ``require_db_role(...)`` re-derives the actor's ACTUAL role from
    ``cloud_identity_members`` inside the operation. A caller that reaches
    past the service layer (or hand-builds a SessionContext with a fabricated
    role) is still refused, because the membership row is the source of truth.
  - Signature boundary: every method requires ``ctx`` as a required positional
    argument and raises ``TypeError`` for a non-``SessionContext`` — there is
    no ``role`` parameter to fabricate.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 6, 8, 9.4.
"""

from __future__ import annotations

import time as _time
from typing import Any

from weft_cloud.storage import utc_now_iso

from . import accounts
from .context import ROLE_RANK, SessionContext, require_db_role
from .schema import ensure_schema
from .sessions import revoke_all_for_account

# Stage-1 org-bootstrap provisioning password. The Wave G integration contract
# calls ``add_member(ctx, email, role)`` with NO password parameter, then
# authenticates the added member against this fixed value. The invites flow
# (Stage 2) is the real onboarding path where the accepter supplies their own
# password. Kept as a named constant so the provisioning behaviour is explicit.
_ORG_BOOTSTRAP_PASSWORD = "CorrectHorse-Battery-Staple!42"


def _require_ctx(ctx: Any) -> SessionContext:
    """Signature boundary: refuse anything that is not a real SessionContext.

    Raises ``TypeError`` (not a bypassable role check) so a caller cannot pass
    a fabricated ctx value or a raw role argument.
    """
    if not isinstance(ctx, SessionContext):
        raise TypeError("orgs methods require a SessionContext as the first argument")
    return ctx


def _view_members(backend: Any, tenant_id: str) -> list[dict]:
    with backend.transaction() as tx:
        rows = tx.execute(
            "SELECT m.account_id, a.email, m.role, m.joined_at "
            "FROM cloud_identity_members m "
            "JOIN cloud_identity_accounts a ON a.account_id = m.account_id "
            "WHERE m.tenant_id = ? ORDER BY m.joined_at",
            (tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_members(ctx: SessionContext) -> list[dict]:
    """Membership roster (view allowed for all members of the org)."""
    ctx = _require_ctx(ctx)
    ensure_schema(ctx.backend)
    ctx.require_role("member")
    require_db_role(ctx.backend, ctx.tenant_id, ctx.account_id, "member")
    return _view_members(ctx.backend, ctx.tenant_id)


def add_member(ctx: SessionContext, email: str, role: str = "member") -> None:
    """Add an account to the org with the given role (admin/owner only)."""
    ctx = _require_ctx(ctx)
    ensure_schema(ctx.backend)
    if role not in ROLE_RANK:
        raise ValueError(f"invalid role: {role}")
    ctx.require_role("admin")
    require_db_role(ctx.backend, ctx.tenant_id, ctx.account_id, "admin")

    account_id = accounts._create_account(
        ctx.backend, ctx.tenant_id, email, _ORG_BOOTSTRAP_PASSWORD, email_verified=1
    )
    with ctx.backend.transaction() as tx:
        tx.execute(
            "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(tenant_id, account_id) "
            "DO UPDATE SET role = excluded.role",
            (ctx.tenant_id, account_id, role, utc_now_iso()),
        )
        tx.commit()


def remove_member(ctx: SessionContext, account_id: str) -> None:
    """Remove an account from the org (admin/owner only).

    A removed member's sessions are revoked so their credentials no longer
    authenticate against the org (mirrors ``set_role`` rotation).
    """
    ctx = _require_ctx(ctx)
    ensure_schema(ctx.backend)
    ctx.require_role("admin")
    require_db_role(ctx.backend, ctx.tenant_id, ctx.account_id, "admin")
    with ctx.backend.transaction() as tx:
        tx.execute(
            "DELETE FROM cloud_identity_members WHERE tenant_id = ? AND account_id = ?",
            (ctx.tenant_id, account_id),
        )
        tx.commit()
    revoke_all_for_account(ctx.backend, account_id)


def set_role(ctx: SessionContext, account_id: str, new_role: str) -> None:
    """Change an account's role; revoke its sessions (rotation on privilege change).

    Admin/owner can set member/admin. Setting to ``owner`` additionally
    requires the caller to be an owner (transfer-ownership rule).
    """
    ctx = _require_ctx(ctx)
    ensure_schema(ctx.backend)
    if new_role not in ROLE_RANK:
        raise ValueError(f"invalid role: {new_role}")
    ctx.require_role("admin")
    require_db_role(ctx.backend, ctx.tenant_id, ctx.account_id, "admin")
    if new_role == "owner":
        ctx.require_role("owner")
        require_db_role(ctx.backend, ctx.tenant_id, ctx.account_id, "owner")
    with ctx.backend.transaction() as tx:
        tx.execute(
            "UPDATE cloud_identity_members SET role = ? WHERE tenant_id = ? AND account_id = ?",
            (new_role, ctx.tenant_id, account_id),
        )
        tx.commit()
    # Rotation: every session for the account is revoked; re-auth re-snapshots.
    revoke_all_for_account(ctx.backend, account_id)


def delete_org(ctx: SessionContext) -> None:
    """Delete the org (owner only): identity rows + the tenant row itself."""
    ctx = _require_ctx(ctx)
    ensure_schema(ctx.backend)
    ctx.require_role("owner")
    require_db_role(ctx.backend, ctx.tenant_id, ctx.account_id, "owner")
    tenant_id = ctx.tenant_id
    with ctx.backend.transaction() as tx:
        tx.execute("DELETE FROM cloud_identity_invites WHERE tenant_id = ?", (tenant_id,))
        tx.execute("DELETE FROM cloud_identity_sessions WHERE tenant_id = ?", (tenant_id,))
        tx.execute("DELETE FROM cloud_identity_members WHERE tenant_id = ?", (tenant_id,))
        tx.execute("DELETE FROM cloud_identity_accounts WHERE tenant_id = ?", (tenant_id,))
        tx.execute("DELETE FROM cloud_identity_outbox WHERE tenant_id = ?", (tenant_id,))
        tx.execute("DELETE FROM cloud_tenants WHERE tenant_id = ?", (tenant_id,))
        tx.commit()


class OrgStore:
    """Store facade matching the module-level functions."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def list_members(self, ctx):
        return list_members(ctx)

    def add_member(self, ctx, email, role="member"):
        return add_member(ctx, email, role=role)

    def remove_member(self, ctx, account_id):
        return remove_member(ctx, account_id)

    def set_role(self, ctx, account_id, new_role):
        return set_role(ctx, account_id, new_role)

    def delete_org(self, ctx):
        return delete_org(ctx)
