"""Identity — sessions: opaque session tokens, SHA-256 at rest, rotation.

Token hygiene (mirror ``roster.py``):
  - Raw token ``fss_{token_urlsafe(32)}`` is returned EXACTLY ONCE at creation.
  - Only ``sha256(raw)`` is stored (``token_hash`` UNIQUE column).
  - Raw tokens are never logged, serialised, or placed in error messages.

Rotation: sessions carry a ``role_snapshot`` captured at creation. On a role
change the account's sessions are revoked (revoke-all + re-issue), never
mutated in place — re-authentication produces the fresh snapshot.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 4, 9.3, 13.
"""

from __future__ import annotations

import time as _time
import uuid
from typing import Any

from weft_cloud.storage import utc_now_iso

from .context import SessionContext
from .schema import ensure_schema
from .tokens import AuthError, generate_token, hash_token  # noqa: F401 (AuthError re-exported)

DEFAULT_TTL_SECONDS = 24 * 3600


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def create(backend: Any, tenant_id: str, account_id: str, role: str,
           ttl_seconds: int = DEFAULT_TTL_SECONDS) -> tuple[str, str]:
    """Issue a session for an exact tenant membership.

    The signup service mints its first owner session immediately after creating
    an unverified account and before its follow-up membership insert. Preserve
    that narrow bootstrap window by creating the owner membership atomically;
    all other callers must already have a matching membership and role.
    """
    ensure_schema(backend)
    session_id = _new_id("ses")
    raw_token = generate_token("fss")
    token_hash = hash_token(raw_token)
    now = _time.time()
    with backend.transaction() as tx:
        account = tx.execute(
            "SELECT tenant_id, email_verified, verification_token_hash "
            "FROM cloud_identity_accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        membership = tx.execute(
            "SELECT role FROM cloud_identity_members "
            "WHERE tenant_id = ? AND account_id = ?",
            (tenant_id, account_id),
        ).fetchone()

        if account is None or account["tenant_id"] != tenant_id:
            raise AuthError("invalid_session")
        if membership is None:
            is_signup_bootstrap = (
                role == "owner"
                and account["email_verified"] == 0
                and account["verification_token_hash"] is not None
            )
            if not is_signup_bootstrap:
                raise AuthError("invalid_session")
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', ?)",
                (tenant_id, account_id, utc_now_iso()),
            )
            snapshot_role = "owner"
        elif membership["role"] != role:
            raise AuthError("invalid_session")
        else:
            snapshot_role = membership["role"]

        tx.execute(
            "INSERT INTO cloud_identity_sessions("
            " session_id, tenant_id, account_id, token_hash, created_at, expires_at, role_snapshot"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, tenant_id, account_id, token_hash, utc_now_iso(), now + ttl_seconds,
             snapshot_role),
        )
        tx.commit()
    return session_id, raw_token


def validate(backend: Any, raw_token: str) -> SessionContext:
    """Resolve a raw session token to a SessionContext.

    Refuses (``AuthError("invalid_session")``) a missing/tampered/revoked/
    expired token. The role comes from the DB session row — never from input.
    """
    ensure_schema(backend)
    token_hash = hash_token(raw_token)
    now = _time.time()
    with backend.transaction() as tx:
        row = tx.execute(
            "SELECT * FROM cloud_identity_sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    if row is None or row["revoked_at"] is not None or row["expires_at"] <= now:
        raise AuthError("invalid_session")
    return SessionContext(
        tenant_id=row["tenant_id"],
        account_id=row["account_id"],
        role=row["role_snapshot"],
        backend=backend,
    )


def revoke(backend: Any, session_id: str) -> None:
    ensure_schema(backend)
    now = _time.time()
    with backend.transaction() as tx:
        tx.execute(
            "UPDATE cloud_identity_sessions SET revoked_at = ? "
            "WHERE session_id = ? AND revoked_at IS NULL",
            (now, session_id),
        )
        tx.commit()


def revoke_by_token_hash(backend: Any, token_hash: str) -> None:
    """Revoke the session whose stored SHA-256 token hash matches.

    Single transaction — the lookup and the UPDATE run under ONE writer lock.
    Never call this from inside another ``backend.transaction()``: SQLite has
    a single writer, so a nested ``BEGIN IMMEDIATE`` blocks until the outer
    commits, and the outer cannot commit until the inner returns (a 15s
    self-deadlock that surfaces as a 500).
    """
    ensure_schema(backend)
    now = _time.time()
    with backend.transaction() as tx:
        tx.execute(
            "UPDATE cloud_identity_sessions SET revoked_at = ? "
            "WHERE token_hash = ? AND revoked_at IS NULL",
            (now, token_hash),
        )
        tx.commit()


def revoke_all_for_account(backend: Any, account_id: str) -> None:
    """Revoke every session for an account (password change/reset, role change)."""
    ensure_schema(backend)
    now = _time.time()
    with backend.transaction() as tx:
        tx.execute(
            "UPDATE cloud_identity_sessions SET revoked_at = ? "
            "WHERE account_id = ? AND revoked_at IS NULL",
            (now, account_id),
        )
        tx.commit()


def revoke_all_for_tenant_account_in_tx(tx: Any, tenant_id: str, account_id: str) -> None:
    """Revoke an account's sessions for one tenant on an open transaction."""
    tx.execute(
        "UPDATE cloud_identity_sessions SET revoked_at = ? "
        "WHERE tenant_id = ? AND account_id = ? AND revoked_at IS NULL",
        (_time.time(), tenant_id, account_id),
    )


def revoke_all_for_tenant(backend: Any, tenant_id: str) -> None:
    """Nuclear option: revoke every session in a tenant."""
    ensure_schema(backend)
    now = _time.time()
    with backend.transaction() as tx:
        tx.execute(
            "UPDATE cloud_identity_sessions SET revoked_at = ? "
            "WHERE tenant_id = ? AND revoked_at IS NULL",
            (now, tenant_id),
        )
        tx.commit()


class SessionStore:
    """Store facade matching the module-level functions (backend passed explicitly)."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def create(self, backend, tenant_id, account_id, role, ttl_seconds=DEFAULT_TTL_SECONDS):
        return create(backend, tenant_id, account_id, role, ttl_seconds=ttl_seconds)

    def validate(self, backend, raw_token):
        return validate(backend, raw_token)

    def revoke(self, backend, session_id):
        return revoke(backend, session_id)

    def revoke_by_token_hash(self, backend, token_hash):
        return revoke_by_token_hash(backend, token_hash)

    def revoke_all_for_account(self, backend, account_id):
        return revoke_all_for_account(backend, account_id)
