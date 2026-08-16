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

from . import invites
from .agent_keys import (
    revoke_all_for_tenant_account_in_tx as revoke_all_agent_keys_for_tenant_account_in_tx,
)
from .context import ROLE_RANK, RoleError, SessionContext, require_db_role
from .schema import ensure_schema
from .sessions import (
    revoke_all_for_tenant_account_in_tx as revoke_all_sessions_for_tenant_account_in_tx,
)


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


def add_member(ctx: SessionContext, email: str, role: str = "member") -> tuple[str, str]:
    """Create an explicit invite instead of provisioning credentials.

    The call shape remains compatible for callers that ignored the old
    ``None`` return value. The returned ``(invite_id, raw_token)`` is the
    onboarding handoff: only ``invites.accept`` may create the account,
    establish membership, set a caller-chosen password, and mark the email
    verified because possession of the single-use invite is proof.
    """
    ctx = _require_ctx(ctx)
    ensure_schema(ctx.backend)
    if role not in ("member", "admin"):
        raise ValueError("new members must use the invite flow; owner transfer is explicit")
    ctx.require_role("admin")
    require_db_role(ctx.backend, ctx.tenant_id, ctx.account_id, "admin")
    return invites.create(ctx, email, role)


def remove_member(ctx: SessionContext, account_id: str) -> None:
    """Remove an account from the org (admin/owner only).

    A removed member's sessions are revoked so their credentials no longer
    authenticate against the org (mirrors ``set_role`` rotation). Owners are
    never silently removed: transfer ownership first, and refuse while the
    account still owns any active rooms so close/revoke authority cannot be
    orphaned.
    """
    ctx = _require_ctx(ctx)
    ensure_schema(ctx.backend)
    ctx.require_role("admin")
    require_db_role(ctx.backend, ctx.tenant_id, ctx.account_id, "admin")
    with ctx.backend.transaction() as tx:
        target = tx.execute(
            "SELECT role FROM cloud_identity_members "
            "WHERE tenant_id = ? AND account_id = ?",
            (ctx.tenant_id, account_id),
        ).fetchone()
        if target is not None and target["role"] == "owner":
            raise RoleError("forbidden")
        key_rows = tx.execute(
            "SELECT key_id FROM cloud_identity_agent_keys "
            "WHERE tenant_id = ? AND account_id = ?",
            (ctx.tenant_id, account_id),
        ).fetchall()
        owner_ids = [account_id, *[row["key_id"] for row in key_rows]]
        owner_placeholders = ",".join("?" for _ in owner_ids)
        active_room = tx.execute(
            "SELECT 1 FROM cloud_rooms WHERE tenant_id = ? "
            "AND owner_agent_id IN (" + owner_placeholders + ") "
            "AND state != 'closed' LIMIT 1",
            (ctx.tenant_id, *owner_ids),
        ).fetchone()
        if active_room is not None:
            raise RoleError("forbidden")
        tx.execute(
            "DELETE FROM cloud_identity_members WHERE tenant_id = ? AND account_id = ?",
            (ctx.tenant_id, account_id),
        )
        revoke_all_agent_keys_for_tenant_account_in_tx(tx, ctx.tenant_id, account_id)
        revoke_all_sessions_for_tenant_account_in_tx(tx, ctx.tenant_id, account_id)
        from weft_cloud.rooms import offboard_account_memberships_in_tx
        offboard_account_memberships_in_tx(tx, ctx.tenant_id, account_id)
        tx.commit()


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
        revoke_all_sessions_for_tenant_account_in_tx(tx, ctx.tenant_id, account_id)
        tx.commit()


def delete_org(ctx: SessionContext) -> None:
    """Delete the org (owner only) and all tenant-scoped state atomically.

    The cloud tables intentionally do not rely on database-wide cascading
    foreign keys: room/event/quota tables are additive and several legacy
    tables have no FK declarations.  Tenant deletion therefore owns an
    explicit, ordered teardown.  Credentials and room state must disappear in
    the same transaction as the tenant row, otherwise a deleted org can leave
    usable keys, occupied seats, or orphaned events behind.
    """
    ctx = _require_ctx(ctx)
    ensure_schema(ctx.backend)
    ctx.require_role("owner")
    require_db_role(ctx.backend, ctx.tenant_id, ctx.account_id, "owner")
    tenant_id = ctx.tenant_id
    with ctx.backend.transaction() as tx:
        # Room descendants have no FK cascade, so remove them explicitly
        # before deleting the tenant's room bindings and quota counters.
        for table in (
            "cloud_room_receipts",
            "cloud_room_group_members",
            "cloud_room_groups",
            "cloud_room_cursors",
            "cloud_room_event_log",
            "cloud_room_links",
            "cloud_room_members",
            "cloud_rooms",
            "cloud_room_counters",
            "cloud_tenant_rooms",
            "cloud_rate_windows",
            "cloud_event_mirror",
            "cloud_outbox",
            "cloud_audit",
            "cloud_counters",
        ):
            tx.execute(f"DELETE FROM {table} WHERE tenant_id = ?", (tenant_id,))

        # Identity FKs are enabled on SQLite.  Remove dependants before
        # accounts; agent keys must be removed (not merely revoked) because
        # the org itself no longer exists and the raw bearer credentials must
        # never survive its deletion.
        for table in (
            "cloud_identity_invites",
            "cloud_identity_sessions",
            "cloud_identity_members",
            "cloud_identity_agent_keys",
            "cloud_identity_accounts",
            "cloud_identity_outbox",
        ):
            tx.execute(f"DELETE FROM {table} WHERE tenant_id = ?", (tenant_id,))
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
