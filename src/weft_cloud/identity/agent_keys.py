"""Identity — agent API keys: long-lived, revocable config-file credentials.

The hosted service's browser sessions die after 24h with no refresh path.
A desktop MCP client holds a STATIC bearer token in a config file and never
signs in, so a session token silently starts collecting 401s mid-conversation.
Agent keys are the credential type for exactly that: long-lived (no expiry
clock), shown raw EXACTLY once at creation, immediately revocable, and scoped
to the tenant + account that created them.

Token hygiene (mirror ``sessions.py``):
  - Raw token ``agk_{token_urlsafe(32)}`` is returned EXACTLY ONCE at creation.
  - Only ``sha256(raw)`` is stored (``token_hash`` UNIQUE column).
  - Raw tokens are never logged, serialised, or placed in error messages.

Role model — NEVER more than the account holds:
  - Sessions snapshot their role at creation. Agent keys do NOT: they are
    long-lived, so a snapshot would let a demoted account keep an admin key
    forever. ``validate`` RE-DERIVES the role from ``cloud_identity_members``
    on every request, so a demotion or a membership removal is reflected on
    the very next request. A key can never carry more than the account
    currently holds.

Revocation is immediate and permanent: ``revoke`` is a conditional UPDATE
scoped to ``(tenant_id, account_id, key_id)`` under one writer lock, and
``validate`` checks ``revoked_at IS NULL`` on every call — a revoked key is
refused on the very next request. Password reset revokes all of an account's
keys (``revoke_all_for_account``), matching the session hygiene.

Authoritative spec: docs/AGENT_KEYS.md.
"""

from __future__ import annotations

import time as _time
import uuid
from typing import Any

from weft_cloud.storage import utc_now_iso

from .context import SessionContext
from .schema import ensure_schema
from .tokens import AuthError, generate_token, hash_token  # noqa: F401 (AuthError re-exported)

#: Prefix for agent API keys — distinct from fss_ (session), rm_ (room link),
#: fst_actor_ (self-hosted actor), fvt_/frt_/fiv_ (one-use identity tokens).
#: Frozen prefixes are never changed; this is a new credential type.
AGENT_KEY_PREFIX = "agk"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def create(backend: Any, tenant_id: str, account_id: str, label: str = "default") -> tuple[str, str]:
    """Issue an agent key; return (key_id, raw_token). Raw token shown once.

    ``label`` is free-form text the owner uses to tell keys apart (which host,
    which machine). It is never secret and never logged with the token.
    """
    ensure_schema(backend)
    # The row id uses a DISTINCT prefix (key_) from the raw credential (agk_),
    # so a listing — which exposes ids — can never be mistaken for, or greebable
    # as, the secret itself.
    key_id = _new_id("key")
    raw_token = generate_token(AGENT_KEY_PREFIX)
    token_hash = hash_token(raw_token)
    with backend.transaction() as tx:
        member = tx.execute(
            "SELECT 1 FROM cloud_identity_members "
            "WHERE tenant_id = ? AND account_id = ?",
            (tenant_id, account_id),
        ).fetchone()
        if member is None:
            raise AuthError("invalid_session")
        tx.execute(
            "INSERT INTO cloud_identity_agent_keys("
            " key_id, tenant_id, account_id, label, token_hash, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (key_id, tenant_id, account_id, label, token_hash, utc_now_iso()),
        )
        tx.commit()
    return key_id, raw_token


def validate(backend: Any, raw_token: str) -> SessionContext:
    """Resolve a raw agent key to a SessionContext.

    Refuses (``AuthError("invalid_session")``) a missing/tampered/unknown/
    revoked key alike — the identical code sessions use, so the shared auth
    funnel cannot be probed for which credential type or key exists. The role
    is RE-DERIVED from ``cloud_identity_members`` on every call; a key whose
    account was demoted or removed from the tenant is refused on the next
    request.
    """
    ensure_schema(backend)
    token_hash = hash_token(raw_token)
    now = _time.time()
    with backend.transaction() as tx:
        row = tx.execute(
            "SELECT * FROM cloud_identity_agent_keys WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise AuthError("invalid_session")
        member = tx.execute(
            "SELECT role FROM cloud_identity_members "
            "WHERE tenant_id = ? AND account_id = ?",
            (row["tenant_id"], row["account_id"]),
        ).fetchone()
        if member is None:
            raise AuthError("invalid_session")
        tx.execute(
            "UPDATE cloud_identity_agent_keys SET last_used_at = ? WHERE key_id = ?",
            (now, row["key_id"]),
        )
        tx.commit()
    # Identity shape: the key's OWN row id is the agent identity. The key row
    # is the server-side source of truth for "which agent is this?", so the
    # identity is derived HERE from the authenticated key row and is never
    # client input. ``key_id`` (``key_<uuid>``) is stable for the life of the
    # key, unique per key (hence within a tenant), and its ``key_`` prefix is a
    # namespace disjoint from account ids (``acct_``), so a key identity can
    # never collide with a session-derived identity. It is display-safe: a
    # random uuid with no relation to the raw token or its SHA-256, the two
    # values this module guarantees never to leak.
    return SessionContext(
        tenant_id=row["tenant_id"],
        account_id=row["account_id"],
        role=member["role"],
        backend=backend,
        _agent_id=row["key_id"],
    )


def revoke(backend: Any, tenant_id: str, account_id: str, key_id: str) -> None:
    """Revoke one key immediately, scoped to its owning tenant + account.

    The conditional UPDATE matches nothing for a key owned by someone else, so
    an account can never revoke another account's key even with the id.
    """
    ensure_schema(backend)
    now = _time.time()
    with backend.transaction() as tx:
        tx.execute(
            "UPDATE cloud_identity_agent_keys SET revoked_at = ? "
            "WHERE key_id = ? AND tenant_id = ? AND account_id = ? AND revoked_at IS NULL",
            (now, key_id, tenant_id, account_id),
        )
        tx.commit()


def list_for_account(backend: Any, tenant_id: str, account_id: str) -> list[dict[str, Any]]:
    """List an account's keys in this tenant: id, label, created, last-used.

    Returns metadata only — never the raw key, never the stored hash.
    """
    ensure_schema(backend)
    with backend.transaction() as tx:
        rows = tx.execute(
            "SELECT key_id, label, created_at, last_used_at, revoked_at "
            "FROM cloud_identity_agent_keys "
            "WHERE tenant_id = ? AND account_id = ? "
            "ORDER BY created_at",
            (tenant_id, account_id),
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_all_for_account(backend: Any, account_id: str) -> None:
    """Revoke every key for an account (password reset/security event).

    Long-lived credentials must die with the account's other secrets on a
    reset, or a reset leaves a working config-file key behind. ``reset_password``
    runs this inside its OWN transaction (never nested — see sessions.py).
    """
    ensure_schema(backend)
    now = _time.time()
    with backend.transaction() as tx:
        tx.execute(
            "UPDATE cloud_identity_agent_keys SET revoked_at = ? "
            "WHERE account_id = ? AND revoked_at IS NULL",
            (now, account_id),
        )
        tx.commit()


def revoke_all_for_tenant_account_in_tx(
    tx: Any, tenant_id: str, account_id: str, now: float | None = None
) -> None:
    """Revoke this account's keys in one tenant using a caller-owned transaction.

    Membership removal uses this helper before its transaction commits, so a
    removed member's long-lived credential cannot survive a crash between the
    membership delete and the credential revocation. The tenant predicate is
    deliberate: the same account may hold valid keys in another tenant.
    """
    if now is None:
        now = _time.time()
    tx.execute(
        "UPDATE cloud_identity_agent_keys SET revoked_at = ? "
        "WHERE tenant_id = ? AND account_id = ? AND revoked_at IS NULL",
        (now, tenant_id, account_id),
    )


def revoke_all_for_tenant(backend: Any, tenant_id: str) -> None:
    """Nuclear option: revoke every agent key in a tenant."""
    ensure_schema(backend)
    now = _time.time()
    with backend.transaction() as tx:
        tx.execute(
            "UPDATE cloud_identity_agent_keys SET revoked_at = ? "
            "WHERE tenant_id = ? AND revoked_at IS NULL",
            (now, tenant_id),
        )
        tx.commit()


class AgentKeyStore:
    """Store facade matching the module-level functions (backend passed explicitly)."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def create(self, backend, tenant_id, account_id, label="default"):
        return create(backend, tenant_id, account_id, label=label)

    def validate(self, backend, raw_token):
        return validate(backend, raw_token)

    def revoke(self, backend, tenant_id, account_id, key_id):
        return revoke(backend, tenant_id, account_id, key_id)

    def list_for_account(self, backend, tenant_id, account_id):
        return list_for_account(backend, tenant_id, account_id)

    def revoke_all_for_account(self, backend, account_id):
        return revoke_all_for_account(backend, account_id)

    def revoke_all_for_tenant(self, backend, tenant_id):
        return revoke_all_for_tenant(backend, tenant_id)
