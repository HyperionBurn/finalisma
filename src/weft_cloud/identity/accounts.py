"""Identity — accounts: signup, password hashing, verification, reset.

Password handling:
  - ``hashlib.scrypt`` (stdlib) with per-user random salt.
  - Constant-time compare via ``hmac.compare_digest``.
  - Raw passwords are NEVER stored, logged, or serialised.
  - Unknown-email auth performs the SAME scrypt computation against a dummy
    salt + hash, so timing is indistinguishable from a wrong password (no
    user enumeration).

Single-use tokens are consumed with a conditional UPDATE + rowcount check
inside one transaction — a race cannot double-consume.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 3, 5, 9.1-9.2.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time as _time
import uuid
from typing import Any

from weft_cloud.storage import utc_now_iso

from .mailer import LocalOutboxMailer
from .schema import ensure_schema
from .tokens import AuthError, generate_token, hash_token  # noqa: F401 (AuthError re-exported)

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64

VERIFY_TTL_SECONDS = 24 * 3600
RESET_TTL_SECONDS = 30 * 60

# Dummy credentials used ONLY for the unknown-email timing invariant: we run
# the real scrypt cost against these and compare against a hash that can never
# match, so an unknown email costs the same as a wrong password.
_DUMMY_SALT = b"\x0b" * 32
_DUMMY_HASH = b"\x00" * 64


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _find_account(backend: Any, tenant_id: str, email: str):
    with backend.transaction() as tx:
        return tx.execute(
            "SELECT * FROM cloud_identity_accounts WHERE tenant_id = ? AND email = ?",
            (tenant_id, email),
        ).fetchone()


def _create_account(backend: Any, tenant_id: str, email: str, password: str,
                    email_verified: int = 0) -> str:
    """Insert an account row (or return the existing id).

    Internal provisioning used by signup (unverified + verification email) and
    by admin/invite flows (verified, no verification email).
    """
    existing = _find_account(backend, tenant_id, email)
    if existing is not None:
        return existing["account_id"]
    account_id = _new_id("acct")
    salt = os.urandom(32)
    password_hash = _scrypt(password, salt)
    with backend.transaction() as tx:
        tx.execute(
            "INSERT INTO cloud_identity_accounts("
            " account_id, tenant_id, email, salt, password_hash, created_at, email_verified"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, tenant_id, email, salt, password_hash, utc_now_iso(), email_verified),
        )
        tx.commit()
    return account_id


def signup(backend: Any, tenant_id: str, email: str, password: str) -> tuple[str, str]:
    """Create an account (and its tenant row if absent); return (account_id, raw_verification_token).

    An email is a person's identity across tenants: if ANY tenant already has
    an account for ``email``, signup RAISES ``AuthError("email_exists")``
    instead of returning the existing account id. Returning the existing id
    let ``handle_signup`` mint a session for an account the caller never
    created — passwordless account takeover. Callers that legitimately need
    to (re)provision an existing identity use ``_create_account`` directly
    (org/invite flows), never this path.
    """
    ensure_schema(backend)
    with backend.transaction() as tx:
        row = tx.execute(
            "SELECT 1 FROM cloud_identity_accounts WHERE email = ?",
            (email,),
        ).fetchone()
    if row is not None:
        raise AuthError("email_exists")
    backend.create_tenant(tenant_id, email, "free")
    account_id = _create_account(backend, tenant_id, email, password, email_verified=0)
    raw_token = generate_token("fvt")
    token_hash = hash_token(raw_token)
    expires_at = _time.time() + VERIFY_TTL_SECONDS
    with backend.transaction() as tx:
        tx.execute(
            "UPDATE cloud_identity_accounts SET verification_token_hash = ?, verification_expires_at = ? "
            "WHERE account_id = ?",
            (token_hash, expires_at, account_id),
        )
        tx.commit()
    LocalOutboxMailer(backend).send(
        tenant_id, email, "Verify your email", f"Verify your email: {raw_token}"
    )
    return account_id, raw_token


def verify_email(backend: Any, verification_token: str) -> None:
    """Single-use email verification (24h expiry). Raises AuthError on reuse/expiry/tamper."""
    ensure_schema(backend)
    token_hash = hash_token(verification_token)
    now = _time.time()
    with backend.transaction() as tx:
        cur = tx.execute(
            "UPDATE cloud_identity_accounts SET email_verified = 1, verification_token_hash = NULL "
            "WHERE verification_token_hash = ? AND verification_expires_at > ? AND email_verified = 0",
            (token_hash, now),
        )
        if cur.rowcount == 0:
            raise AuthError("invalid_token")
        tx.commit()


def authenticate(backend: Any, tenant_id: str, email: str, password: str) -> str:
    """Return account_id on success; refuse on wrong/unknown. Timing-invariant for unknown email."""
    ensure_schema(backend)
    row = _find_account(backend, tenant_id, email)
    if row is None:
        # Same scrypt cost as the wrong-password path; the compare always fails.
        computed = _scrypt(password, _DUMMY_SALT)
        hmac.compare_digest(computed, _DUMMY_HASH)
        raise AuthError("invalid_credentials")
    computed = _scrypt(password, row["salt"])
    if not hmac.compare_digest(computed, row["password_hash"]):
        raise AuthError("invalid_credentials")
    return row["account_id"]


def burn_scrypt_cost(password: str) -> None:
    """Run the same scrypt cost as an unknown-email authentication attempt.

    HTTP signin surfaces resolve the tenant BEFORE authenticating. On a
    tenant-lookup miss they must still spend the scrypt work, or the early
    return becomes a user-enumeration timing oracle (known-email-wrong-password
    costs one scrypt, unknown-email returns instantly). This performs exactly
    the dummy-salt computation ``authenticate`` would have run for an unknown
    email, so both refusal paths cost the same.
    """
    computed = _scrypt(password, _DUMMY_SALT)
    hmac.compare_digest(computed, _DUMMY_HASH)


def request_password_reset(backend: Any, tenant_id: str, email: str) -> None:
    """Enqueue a reset email with an frt_ token. Always succeeds silently (no enumeration)."""
    ensure_schema(backend)
    row = _find_account(backend, tenant_id, email)
    if row is None:
        return
    raw_token = generate_token("frt")
    token_hash = hash_token(raw_token)
    expires_at = _time.time() + RESET_TTL_SECONDS
    with backend.transaction() as tx:
        tx.execute(
            "UPDATE cloud_identity_accounts SET reset_token_hash = ?, reset_expires_at = ? "
            "WHERE account_id = ?",
            (token_hash, expires_at, row["account_id"]),
        )
        tx.commit()
    LocalOutboxMailer(backend).send(
        tenant_id, email, "Reset your password", f"Reset your password: {raw_token}"
    )


def reset_password(backend: Any, reset_token: str, new_password: str) -> None:
    """Single-use password reset (30-min expiry). Revokes ALL of the account's sessions.

    The password change and the session revocation are in the SAME transaction,
    so a crash cannot leave live sessions behind an old password.
    """
    ensure_schema(backend)
    token_hash = hash_token(reset_token)
    now = _time.time()
    with backend.transaction() as tx:
        row = tx.execute(
            "SELECT account_id FROM cloud_identity_accounts WHERE reset_token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise AuthError("invalid_token")
        salt = os.urandom(32)
        new_hash = _scrypt(new_password, salt)
        cur = tx.execute(
            "UPDATE cloud_identity_accounts SET password_hash = ?, salt = ?, "
            " reset_token_hash = NULL, reset_expires_at = 0 "
            "WHERE account_id = ? AND reset_token_hash = ? AND reset_expires_at > ?",
            (new_hash, salt, row["account_id"], token_hash, now),
        )
        if cur.rowcount == 0:
            raise AuthError("invalid_token")
        tx.execute(
            "UPDATE cloud_identity_sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
            (now, row["account_id"]),
        )
        tx.commit()


def get_by_id(backend: Any, account_id: str) -> dict | None:
    """Safe account projection — no password/token material in the returned dict."""
    ensure_schema(backend)
    with backend.transaction() as tx:
        row = tx.execute(
            "SELECT account_id, tenant_id, email, email_verified, created_at "
            "FROM cloud_identity_accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return dict(row) if row else None


class AccountStore:
    """Store facade matching the module-level functions (backend passed explicitly)."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def signup(self, backend, tenant_id, email, password):
        return signup(backend, tenant_id, email, password)

    def authenticate(self, backend, tenant_id, email, password):
        return authenticate(backend, tenant_id, email, password)

    def burn_scrypt_cost(self, password):
        return burn_scrypt_cost(password)

    def verify_email(self, backend, verification_token):
        return verify_email(backend, verification_token)

    def request_password_reset(self, backend, tenant_id, email):
        return request_password_reset(backend, tenant_id, email)

    def reset_password(self, backend, reset_token, new_password):
        return reset_password(backend, reset_token, new_password)

    def get_by_id(self, backend, account_id):
        return get_by_id(backend, account_id)
