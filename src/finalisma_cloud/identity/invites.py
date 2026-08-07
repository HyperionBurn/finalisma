"""Identity — invites: expiring, single-use, role-scoped, email-locked.

Invites are the Stage-1 onboarding path. Invariants (design §7.3):
  1. The granted role is EXACTLY the invite's role — the accepter never
     supplies a role, so there is no self-escalation.
  2. The accepter must present the invite's email — a mismatched email is
     refused without consuming the token.
  3. The membership lands in the invite's tenant — the accepter cannot
     redirect to another org.
  4. Single-use is enforced by a conditional UPDATE + rowcount check inside
     one transaction — a race cannot double-consume.
  5. Expired/reused tokens are refused uniformly (``invite_expired`` /
     ``invite_consumed``); unknown tokens raise ``invite_expired`` so the
     endpoint is not a token oracle.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 7, 9.5, 13.
"""

from __future__ import annotations

import os
import time as _time
import uuid
from typing import Any

from finalisma_cloud.storage import utc_now_iso

from . import accounts
from .context import SessionContext, require_db_role
from .mailer import LocalOutboxMailer
from .schema import ensure_schema
from .tokens import AuthError, generate_token, hash_token

INVITE_TTL_SECONDS = 7 * 24 * 3600

_INVITE_ROLES = ("admin", "member")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _require_ctx(ctx: Any) -> SessionContext:
    if not isinstance(ctx, SessionContext):
        raise TypeError("invites methods require a SessionContext as the first argument")
    return ctx


def create(ctx: SessionContext, email: str, role: str) -> tuple[str, str]:
    """Create a role-scoped invite for ``email``; return (invite_id, raw_token).

    Admin/owner only (layer 1 + layer 2). ``role`` must be admin or member —
    an owner cannot be invited, so there is no owner-invite escalation path.
    """
    ctx = _require_ctx(ctx)
    ensure_schema(ctx.backend)
    if role not in _INVITE_ROLES:
        raise AuthError("invalid_role")
    ctx.require_role("admin")
    require_db_role(ctx.backend, ctx.tenant_id, ctx.account_id, "admin")

    invite_id = _new_id("inv")
    raw_token = generate_token("fiv")
    token_hash = hash_token(raw_token)
    expires_at = _time.time() + INVITE_TTL_SECONDS
    with ctx.backend.transaction() as tx:
        tx.execute(
            "INSERT INTO cloud_identity_invites("
            " invite_id, tenant_id, email, role, token_hash, created_at, expires_at, created_by"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (invite_id, ctx.tenant_id, email, role, token_hash, utc_now_iso(),
             expires_at, ctx.account_id),
        )
        tx.commit()
    LocalOutboxMailer(ctx.backend).send(
        ctx.tenant_id, email, "You're invited", f"You're invited to join: {raw_token}"
    )
    return invite_id, raw_token


def accept(backend: Any, raw_token: str, email: str, password: str) -> tuple[str, str]:
    """Redeem an invite; return (account_id, session_token).

    Creates the account if new (verified — the invite proves email ownership),
    adds a membership with EXACTLY the invite's role, consumes the token
    atomically, and issues a session.
    """
    ensure_schema(backend)
    token_hash = hash_token(raw_token)
    now = _time.time()
    with backend.transaction() as tx:
        invite = tx.execute(
            "SELECT * FROM cloud_identity_invites WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if invite is None:
            raise AuthError("invite_expired")  # uniform — no token oracle
        if invite["consumed_at"] is not None:
            raise AuthError("invite_consumed")
        if invite["expires_at"] <= now:
            raise AuthError("invite_expired")
        if invite["email"] != email:
            raise AuthError("invite_mismatch")

        # One-org-per-account (design §8/§9.2 #15): an email that already
        # belongs to any org must not be admitted to a second one, even when
        # the two orgs are different tenants. The email is the person's
        # identity across tenants, so refuse before creating an account here.
        existing_member = tx.execute(
            "SELECT 1 FROM cloud_identity_members m "
            "JOIN cloud_identity_accounts a ON a.account_id = m.account_id "
            "WHERE a.email = ? LIMIT 1",
            (email,),
        ).fetchone()
        if existing_member is not None:
            raise AuthError("already_in_org")

        # Consume atomically — single-use guard.
        cur = tx.execute(
            "UPDATE cloud_identity_invites SET consumed_at = ? "
            "WHERE invite_id = ? AND consumed_at IS NULL AND expires_at > ?",
            (now, invite["invite_id"], now),
        )
        if cur.rowcount == 0:
            raise AuthError("invite_consumed")

        tenant_id = invite["tenant_id"]
        existing = tx.execute(
            "SELECT account_id FROM cloud_identity_accounts WHERE tenant_id = ? AND email = ?",
            (tenant_id, email),
        ).fetchone()
        if existing is not None:
            account_id = existing["account_id"]
        else:
            account_id = _new_id("acct")
            salt = os.urandom(32)
            password_hash = accounts._scrypt(password, salt)
            tx.execute(
                "INSERT INTO cloud_identity_accounts("
                " account_id, tenant_id, email, salt, password_hash, created_at, email_verified"
                ") VALUES (?, ?, ?, ?, ?, ?, 1)",
                (account_id, tenant_id, email, salt, password_hash, utc_now_iso()),
            )
        tx.execute(
            "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(tenant_id, account_id) "
            "DO UPDATE SET role = excluded.role",
            (tenant_id, account_id, invite["role"], utc_now_iso()),
        )
        # Issue a session for the new member with the invite's role — inline in
        # the SAME transaction (a nested transaction would deadlock the write lock).
        session_token = generate_token("fss")
        token_hash = hash_token(session_token)
        session_id = _new_id("ses")
        tx.execute(
            "INSERT INTO cloud_identity_sessions("
            " session_id, tenant_id, account_id, token_hash, created_at, expires_at, role_snapshot"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, tenant_id, account_id, token_hash, utc_now_iso(),
             now + 24 * 3600, invite["role"]),
        )
        tx.commit()
    return account_id, session_token


class InviteStore:
    """Store facade matching the module-level functions."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def create(self, ctx, email, role):
        return create(ctx, email, role)

    def accept(self, backend, raw_token, email, password):
        return accept(backend, raw_token, email, password)
