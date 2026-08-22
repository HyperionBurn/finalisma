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
import re
import time as _time
import uuid
from typing import Any
from urllib.parse import quote

from weft_cloud.origin import configured_origin
from weft_cloud.storage import utc_now_iso

from .mailer import LocalOutboxMailer
from .schema import ensure_schema
from .tokens import AuthError, generate_token, hash_token  # noqa: F401 (AuthError re-exported)

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 256

VERIFY_TTL_SECONDS = 24 * 3600
RESET_TTL_SECONDS = 30 * 60

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_DEFAULT_WEB_PUBLIC_ORIGIN = "http://127.0.0.1:18789"

# Dummy credentials used ONLY for the unknown-email timing invariant: we run
# the real scrypt cost against these and compare against a hash that can never
# match, so an unknown email costs the same as a wrong password.
_DUMMY_SALT = b"\x0b" * 32
_DUMMY_HASH = b"\x00" * 64


def validate_password(password: object) -> None:
    """Enforce one password-size policy before hashing or token consumption."""
    if not isinstance(password, str) or not password:
        raise ValueError("password is required")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"password must be at least {PASSWORD_MIN_LENGTH} characters")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"password must be at most {PASSWORD_MAX_LENGTH} characters")


def canonicalize_email(email: object) -> str:
    """Return the one stored and compared form of an email address.

    Identity lookups must not depend on display casing or accidental form
    whitespace. Syntax validation is deliberately separate so old internal
    fixtures and migration rows can still be normalized without being treated
    as interactive customer input.
    """
    if not isinstance(email, str):
        raise ValueError("email is required")
    canonical = email.strip().casefold()
    if not canonical:
        raise ValueError("email is required")
    return canonical


def validate_email(email: object) -> str:
    """Canonicalize and validate an email supplied by a customer-facing API."""
    canonical = canonicalize_email(email)
    if not _EMAIL_RE.fullmatch(canonical):
        raise ValueError("email must be a valid email address")
    return canonical


def _verification_web_origin() -> str:
    """Resolve the origin used for human verification links."""
    return configured_origin(
        env_names=("WEFT_WEB_PUBLIC_ORIGIN", "WEFT_PUBLIC_ORIGIN"),
        default=_DEFAULT_WEB_PUBLIC_ORIGIN,
    )


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
    email = canonicalize_email(email)
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
    email = canonicalize_email(email)
    validate_password(password)
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
    email = canonicalize_email(email)
    validate_password(password)
    ensure_schema(backend)
    raw_token = generate_token("fvt")
    token_hash = hash_token(raw_token)
    expires_at = _time.time() + VERIFY_TTL_SECONDS
    account_id = _new_id("acct")
    salt = os.urandom(32)
    password_hash = _scrypt(password, salt)
    now_iso = utc_now_iso()
    with backend.transaction() as tx:
        # The global-email read and every signup write share one SQLite writer
        # transaction. At transaction entry, no competing signup can pass the
        # check until this transaction commits or rolls back.
        row = tx.execute(
            "SELECT 1 FROM cloud_identity_accounts WHERE email = ?",
            (email,),
        ).fetchone()
        if row is not None:
            raise AuthError("email_exists")
        tx.execute(
            "INSERT OR IGNORE INTO cloud_tenants(tenant_id, name, plan_id, created_at) "
            "VALUES (?, ?, 'free', ?)",
            (tenant_id, email, now_iso),
        )
        tx.execute(
            "INSERT INTO cloud_identity_accounts("
            " account_id, tenant_id, email, salt, password_hash, created_at, "
            " email_verified, verification_token_hash, verification_expires_at"
            ") VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (account_id, tenant_id, email, salt, password_hash, now_iso,
             token_hash, expires_at),
        )
        tx.execute(
            "INSERT INTO cloud_identity_outbox(entry_id, tenant_id, to_email, subject, body, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_new_id("idem"), tenant_id, email, "Verify your email",
             "Verify your email: "
             f"{_verification_web_origin()}/verify?token={quote(raw_token, safe='')}",
             now_iso),
        )
        tx.commit()
    return account_id, raw_token


def leave_membership(backend: Any, tenant_id: str, account_id: str) -> None:
    """Remove a non-owner membership and revoke only its tenant credentials.

    The operation is atomic with room offboarding. Owners are refused because
    leaving would violate the existing ownership contract; callers must use an
    explicit ownership transfer or org deletion flow instead.
    """
    ensure_schema(backend)
    from .agent_keys import (
        revoke_all_for_tenant_account_in_tx as revoke_all_agent_keys_for_tenant_account_in_tx,
    )
    from .context import RoleError
    from .sessions import (
        revoke_all_for_tenant_account_in_tx as revoke_all_sessions_for_tenant_account_in_tx,
    )

    with backend.transaction() as tx:
        membership = tx.execute(
            "SELECT role FROM cloud_identity_members "
            "WHERE tenant_id = ? AND account_id = ?",
            (tenant_id, account_id),
        ).fetchone()
        if membership is None:
            raise AuthError("invalid_session")
        if membership["role"] == "owner":
            raise RoleError("owner_cannot_leave")

        key_rows = tx.execute(
            "SELECT key_id FROM cloud_identity_agent_keys "
            "WHERE tenant_id = ? AND account_id = ?",
            (tenant_id, account_id),
        ).fetchall()
        owner_ids = [account_id, *[row["key_id"] for row in key_rows]]
        placeholders = ",".join("?" for _ in owner_ids)
        active_room = tx.execute(
            "SELECT 1 FROM cloud_rooms WHERE tenant_id = ? "
            "AND owner_agent_id IN (" + placeholders + ") "
            "AND state != 'closed' LIMIT 1",
            (tenant_id, *owner_ids),
        ).fetchone()
        if active_room is not None:
            raise RoleError("active_room_cannot_leave")

        tx.execute(
            "DELETE FROM cloud_identity_members WHERE tenant_id = ? AND account_id = ?",
            (tenant_id, account_id),
        )
        revoke_all_agent_keys_for_tenant_account_in_tx(tx, tenant_id, account_id)
        revoke_all_sessions_for_tenant_account_in_tx(tx, tenant_id, account_id)
        from weft_cloud.rooms import offboard_account_memberships_in_tx
        offboard_account_memberships_in_tx(tx, tenant_id, account_id)
        tx.commit()


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
    email = canonicalize_email(email)
    validate_password(password)
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
    validate_password(password)
    computed = _scrypt(password, _DUMMY_SALT)
    hmac.compare_digest(computed, _DUMMY_HASH)


def request_password_reset(backend: Any, tenant_id: str, email: str) -> None:
    """Enqueue a reset email with an frt_ token. Always succeeds silently (no enumeration)."""
    email = canonicalize_email(email)
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
    """Single-use password reset (30-min expiry). Revokes ALL of the account's
    sessions AND agent API keys.

    The password change and the credential revocations are in the SAME
    transaction, so a crash cannot leave live credentials behind an old
    password. Agent keys are long-lived config-file credentials; a reset that
    left them alive would hand the new password holder a working key minted
    under the old one.
    """
    validate_password(new_password)
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
        key_rows = tx.execute(
            "SELECT tenant_id, key_id FROM cloud_identity_agent_keys "
            "WHERE account_id = ? AND revoked_at IS NULL",
            (row["account_id"],),
        ).fetchall()
        tx.execute(
            "UPDATE cloud_identity_agent_keys SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
            (now, row["account_id"]),
        )
        if key_rows:
            from weft_cloud.rooms import release_agent_key_seats_in_tx
            for key_row in key_rows:
                release_agent_key_seats_in_tx(tx, key_row["tenant_id"], key_row["key_id"])
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
