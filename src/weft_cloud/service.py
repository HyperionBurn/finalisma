"""Weft Cloud HTTP service — the hosted SaaS surface.

This is the runnable HTTP service that binds the identity plane (accounts,
sessions, orgs) and the room plane (multi-use links, ordered event log,
addressing) into a product a person can sign up for and paste a link into N
agents.

Architecture (per docs/PRODUCT_ROADMAP.md §1):
  - ``src/weft_mcp/`` stays stdlib-only forever — UNTOUCHED by this module.
  - ``src/weft_cloud/`` MAY take dependencies, but this service stays
    stdlib-only to keep the dependency-free promise intact for v1.
  - HTTP transport uses the same idiom as ``weft_mcp/server.py``:
    ``http.server.ThreadingHTTPServer`` + ``BaseHTTPRequestHandler``.

The service is a thin JSON-RPC-over-HTTP layer. Every request authenticates
via a Bearer cloud credential (``fss_`` session or ``agk_`` agent key) except
signup/signin. Room mutations
require the actor to be an active member; room reads are member-only.

One link, many agents: the room link (``/r/{room_id}#{token}``) is multi-use
up to the room cap. Any number of distinct agents can redeem it.
"""

from __future__ import annotations

import html
import hmac
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http import HTTPStatus
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from weft_cloud.identity import (
    AccountStore,
    AgentKeyStore,
    AuthError,
    InviteStore,
    OrgStore,
    RoleError,
    SessionContext,
    SessionStore,
    ensure_identity_schema,
)
from weft_cloud.identity.accounts import validate_email, validate_password
from weft_cloud.identity.schema import ensure_schema as _ensure_identity_schema
from weft_cloud.identity.sessions import DEFAULT_TTL_SECONDS
from weft_cloud.mcp import (
    HostedMCPAuthError,
    HostedMCPDispatcher,
    MAX_JSON_RPC_BYTES,
    _FORBIDDEN_IDENTITY_ARGS,
    _MCP_NOTIFICATION_METHODS,
    _json_rpc_error,
    _request_too_large_error,
    _wait_slots,
)
from weft_cloud.quotas import DEFAULT_ROOM_CAP, QuotaError
from weft_cloud.rate_limit import RateLimitedError, enforce_auth_rate_limit
from weft_cloud.rooms import CloudRoomService, RoomError, public_origin
from weft_cloud.storage import SqliteWalBackend, StorageBackend
from weft_cloud.web.security_headers import security_headers

# Secrets are never logged. Tokens are hashed at rest, never stored raw.
# The service never echoes a raw token or password in any response or error.


class _ServiceError(Exception):
    """Internal error carrying an HTTP status and a machine-readable code."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class _HostedMCPRateLimiter:
    """Bound hosted MCP bursts for a single-node preview deployment.

    This is intentionally process-local. Public multi-instance deployments
    still require a distributed edge/store-backed limiter.
    """

    def __init__(self, limit: int = 120, window_seconds: int = 60,
                 max_concurrent: int = 16) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_concurrent = max_concurrent
        self._lock = threading.Lock()
        self._events: dict[str, list[float]] = {}
        self._concurrent: dict[str, int] = {}

    def allow(self, key: str, reserve: bool = False) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            if reserve and self._concurrent.get(key, 0) >= self.max_concurrent:
                return False, 5
            recent = [
                stamp for stamp in self._events.get(key, [])
                if stamp > now - self.window_seconds
            ]
            if len(recent) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - recent[0])))
                self._events[key] = recent
                return False, retry_after
            recent.append(now)
            self._events[key] = recent
            if reserve:
                self._concurrent[key] = self._concurrent.get(key, 0) + 1
            return True, 0

    def release(self, key: str) -> None:
        with self._lock:
            current = self._concurrent.get(key, 0)
            if current <= 1:
                self._concurrent.pop(key, None)
            else:
                self._concurrent[key] = current - 1


class _CloudMetrics:
    """Small process-local Prometheus exporter for cloud HTTP operations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}

    def inc(self, name: str, labels: Mapping[str, str] | None = None) -> None:
        labels = labels or {}
        safe_labels = tuple(
            sorted(
                (
                    str(key),
                    str(value).replace("\\", "\\\\").replace('"', '\\"'),
                )
                for key, value in labels.items()
            )
        )
        with self._lock:
            key = (name, safe_labels)
            self._counters[key] = self._counters.get(key, 0) + 1

    def render(self) -> str:
        with self._lock:
            rows = list(self._counters.items())
        lines = [
            "# HELP weft_http_responses_total Weft cloud HTTP responses by status.",
            "# TYPE weft_http_responses_total counter",
        ]
        for (name, labels), value in sorted(rows):
            label_text = "" if not labels else "{" + ",".join(
                f'{key}="{value}"' for key, value in labels
            ) + "}"
            lines.append(f"{name}{label_text} {value}")
        return "\n".join(lines) + "\n"


def _json_response(status: int, payload: dict[str, Any]) -> tuple[int, bytes]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return status, body


def _read_body(handler: BaseHTTPRequestHandler, max_bytes: int = 1_048_576) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length <= 0:
        raise _ServiceError("invalid_body", "Invalid request size", HTTPStatus.BAD_REQUEST)
    if length > max_bytes:
        # Do not leave an oversized request body on a keep-alive connection.
        # The client may still be writing when the 400 is produced, and an
        # unread body can turn the intended JSON refusal into a Windows
        # ConnectionAbortedError. Drain a moderate oversize request completely
        # and only drain a bounded prefix for pathological declarations, then
        # close the connection so an attacker cannot force an unbounded read.
        handler.close_connection = True
        remaining = min(length, max_bytes * 8)
        try:
            while remaining:
                chunk = handler.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, TimeoutError):
            pass
        raise _ServiceError("invalid_body", "Invalid request size", HTTPStatus.BAD_REQUEST)
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise _ServiceError("invalid_json", "Request body must be JSON", HTTPStatus.BAD_REQUEST)
    if not isinstance(data, dict):
        raise _ServiceError("invalid_body", "Request body must be an object", HTTPStatus.BAD_REQUEST)
    return data


def _reject_json_constant(value: str) -> Any:
    """Reject Python's non-standard JSON constants at the HTTP boundary."""
    raise ValueError(f"non-standard JSON constant: {value}")


def _strict_json_loads(raw: bytes | str) -> Any:
    """Decode JSON while rejecting NaN and both Infinity spellings."""
    return json.loads(raw, parse_constant=_reject_json_constant)


def _bearer_token(handler: BaseHTTPRequestHandler) -> str | None:
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def _html_esc(value: Any) -> str:
    """HTML-escape a value. Never render unescaped data into a page."""
    return html.escape(str(value) if value is not None else "")


def _html_page(title: str, body_html: str) -> bytes:
    """Minimal readable HTML page for the human-facing /j/<token> view."""
    document = (
        '<!DOCTYPE html>\n'
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>'
        'body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;'
        'max-width:56rem;margin:0 auto;padding:1rem;line-height:1.45;'
        'overflow-wrap:anywhere;}'
        'pre{overflow-x:auto;max-width:100%;}'
        'code{overflow-wrap:anywhere;word-break:break-word;}'
        '</style>'
        f'<title>{_html_esc(title)}</title></head>\n'
        f'<body>{body_html}</body></html>'
    )
    return document.encode("utf-8")


# A link token is `rm_` plus url-safe base64, so [A-Za-z0-9_-] covers it.
_JOIN_LINK_RE = re.compile(r"^/j/([A-Za-z0-9_-]+)$")


class WeftCloudService:
    """The hosted SaaS: identity + rooms over one HTTP surface.

    Constructed with a storage backend. All state lives in that backend's
    SQLite-WAL file. Tenancy is structural — the backend scopes every query
    by ``WHERE tenant_id = ?``.
    """

    def __init__(self, backend: StorageBackend, origin: str | None = None,
                 auth_rate_limits: Mapping[str, Any] | None = None,
                 metrics_token: str | None = None) -> None:
        self.backend = backend
        self.origin = public_origin(origin)
        self.auth_rate_limits = auth_rate_limits
        configured_metrics_token = (
            metrics_token
            if metrics_token is not None
            else os.environ.get("WEFT_METRICS_TOKEN")
        )
        self.metrics_token = (
            configured_metrics_token.strip()
            if isinstance(configured_metrics_token, str)
            and configured_metrics_token.strip()
            else None
        )
        self.metrics = _CloudMetrics()
        self.accounts = AccountStore(backend)
        self.sessions = SessionStore(backend)
        self.agent_keys = AgentKeyStore(backend)
        self.orgs = OrgStore(backend)
        self.invites = InviteStore(backend)
        self.rooms = CloudRoomService(backend)
        self.mcp_rate_limiter = _HostedMCPRateLimiter()
        # A higher global circuit breaker protects the process when many
        # authenticated identities arrive through one reverse proxy. The
        # primary limiter below is keyed by the authenticated tenant/agent;
        # this separate breaker is deliberately looser and is not the user
        # fairness mechanism.
        self.mcp_ip_rate_limiter = _HostedMCPRateLimiter(
            limit=600, window_seconds=60, max_concurrent=64,
        )
        # Ensure identity + room schema exist.
        _ensure_identity_schema(backend)
        self.rooms._ensure_room_schema()

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def resolve_identity(self, token: str | None) -> SessionContext:
        """One authentication funnel, two credential types.

        A bearer token resolves to the SAME ``SessionContext`` whether it is an
        ``fss_`` session or an ``agk_`` agent key; from here every handler flows
        through the identical authorization, tenant-confinement, rate-limit,
        and quota checks. Any bad token raises ``AuthError("invalid_session")``
        regardless of its type or shape, so the funnel leaks nothing about
        which credential type exists, which key is registered, or which was
        revoked.
        """
        if not isinstance(token, str) or not token:
            raise AuthError("invalid_session")
        try:
            return self.sessions.validate(self.backend, token)
        except AuthError:
            return self.agent_keys.validate(self.backend, token)

    def readiness_status(self) -> dict[str, str]:
        """Verify the storage path needed to serve authenticated traffic."""
        with self.backend.transaction() as tx:
            tx.execute("SELECT 1").fetchone()
        return {"status": "ready", "service": "weft-cloud"}

    def _authenticate(self, handler: BaseHTTPRequestHandler) -> SessionContext:
        token = _bearer_token(handler)
        if not token:
            raise _ServiceError("unauthorized", "Authorization Bearer token required",
                                HTTPStatus.UNAUTHORIZED)
        try:
            return self.resolve_identity(token)
        except AuthError as exc:
            raise _ServiceError(exc.code, "Invalid session", HTTPStatus.UNAUTHORIZED)

    def _authenticate_session(self, handler: BaseHTTPRequestHandler) -> SessionContext:
        """Session-only auth for key MANAGEMENT endpoints.

        Agent-key create/list/revoke are interactive identity operations: a
        leaked agent key must not be able to mint more keys or revoke the
        owner's credentials (lockout). Only a live interactive session may
        manage keys, so an agent key presented here is refused exactly like any
        other invalid session.
        """
        token = _bearer_token(handler)
        if not token:
            raise _ServiceError("unauthorized", "Authorization Bearer token required",
                                HTTPStatus.UNAUTHORIZED)
        try:
            return self.sessions.validate(self.backend, token)
        except AuthError as exc:
            raise _ServiceError(exc.code, "Invalid session", HTTPStatus.UNAUTHORIZED)

    # ------------------------------------------------------------------
    # Agent-key endpoints — SESSION-ONLY management, long-lived credentials
    # ------------------------------------------------------------------

    def handle_create_agent_key(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate_session(handler)
        body = _read_body(handler)
        label = body.get("label", "default")
        if not isinstance(label, str):
            raise _ServiceError("invalid_argument", "label must be a string")
        label = label.strip()[:64] or "default"
        key_id, raw_token = self.agent_keys.create(
            self.backend, ctx.tenant_id, ctx.account_id, label
        )
        from weft_cloud.storage import utc_now_iso as _utc
        self.backend.append_audit(
            ctx.tenant_id, "agent_key.create", ctx.account_id, key_id,
            json.dumps({"label": label}),
        )
        # The raw token is returned EXACTLY ONCE here. It is never retrievable
        # afterwards; the DB holds only its SHA-256 digest.
        return _json_response(HTTPStatus.CREATED, {
            "key_id": key_id,
            "label": label,
            "agent_key": raw_token,
            "created_at": _utc(),
        })

    def handle_list_agent_keys(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate_session(handler)
        keys = self.agent_keys.list_for_account(self.backend, ctx.tenant_id, ctx.account_id)
        return _json_response(HTTPStatus.OK, {"keys": keys})

    def handle_revoke_agent_key(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate_session(handler)
        body = _read_body(handler)
        key_id = body.get("key_id")
        if not isinstance(key_id, str) or not key_id:
            raise _ServiceError("invalid_argument", "key_id is required")
        self.agent_keys.revoke(self.backend, ctx.tenant_id, ctx.account_id, key_id)
        self.backend.append_audit(
            ctx.tenant_id, "agent_key.revoke", ctx.account_id, key_id, "{}",
        )
        return _json_response(HTTPStatus.OK, {"revoked": True})


    # ------------------------------------------------------------------
    # Identity endpoints
    # ------------------------------------------------------------------

    def handle_signup(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        body = _read_body(handler)
        email = body.get("email")
        password = body.get("password")
        # A client may NEVER choose its tenant: doing so let a caller claim an
        # existing tenant as 'owner' and then mint sessions inside it. Signup
        # always provisions a fresh tenant for the new account.
        tenant_id = f"tenant_{secrets.token_hex(8)}"
        if not isinstance(email, str) or not email:
            raise _ServiceError("invalid_argument", "email and password are required")
        if not isinstance(password, str) or not password:
            raise _ServiceError("invalid_argument", "email and password are required")
        try:
            validate_password(password)
        except ValueError as exc:
            raise _ServiceError("invalid_argument", str(exc)) from exc
        try:
            email = validate_email(email)
        except ValueError as exc:
            raise _ServiceError("invalid_argument", str(exc)) from exc
        if len(password) < 8:
            raise _ServiceError("invalid_argument", "password must be at least 8 characters")

        # Public endpoint: signup mints a tenant + writes rows, so it is both a
        # storage-exhaustion vector and a mail-bomb vector (verification
        # outbox). Enforce the shared auth limiter BEFORE any account work —
        # keyed on the client IP and the requested email, so unlimited account
        # creation from one source and repeated attempts at one address are
        # both refused. Refusals are identical for any email: no oracle.
        enforce_auth_rate_limit(self.backend, handler, "signup", email=email,
                                limits=self.auth_rate_limits)

        try:
            account_id, _verification_token = self.accounts.signup(
                self.backend, tenant_id, email, password
            )
        except AuthError as exc:
            # Match web/app.py:429 — an existing email is refused, and the
            # caller never receives a session (or any other account's id).
            if exc.code == "email_exists":
                raise _ServiceError("email_exists",
                                    "An account with this email already exists",
                                    HTTPStatus.BAD_REQUEST)
            raise
        # The session is issued immediately so the signup -> create-room flow
        # works in one call, but it does NOT imply a verified email: the
        # verification token is left unconsumed in the outbox and the account
        # row stays email_verified=0 until the address is actually proven.
        # Ownership of the email is never asserted by signup itself. The
        # response exposes email_verified so the caller cannot mistake the
        # session for proof of a verified address.
        session_id, session_token = self.sessions.create(
            self.backend, tenant_id, account_id, "owner"
        )
        # Bootstrap: the owner is a member of their own org.
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', datetime('now'))",
                (tenant_id, account_id),
            )
            tx.commit()
        return _json_response(HTTPStatus.CREATED, {
            "account_id": account_id,
            "tenant_id": tenant_id,
            "session_token": session_token,
            "email": email,
            "role": "owner",
            "email_verified": False,
        })

    def handle_signin(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        body = _read_body(handler)
        email = body.get("email")
        password = body.get("password")
        if not isinstance(email, str) or not email:
            raise _ServiceError("invalid_argument", "email and password are required")
        if not isinstance(password, str) or not password:
            raise _ServiceError("invalid_argument", "email and password are required")
        try:
            email = validate_email(email)
        except ValueError as exc:
            raise _ServiceError("invalid_argument", str(exc)) from exc
        try:
            validate_password(password)
        except ValueError as exc:
            raise _ServiceError("invalid_argument", str(exc)) from exc
        # Enforce the shared auth limiter BEFORE the tenant lookup. Both tiers
        # (client IP and the email, counted regardless of existence) run before
        # any branch that depends on whether the account exists, so a throttled
        # known email and a throttled unknown email return the byte-identical
        # 429 — the limit is not an account-existence oracle. The limiter's
        # work is identical for both, so the known-vs-unknown scrypt timing
        # equalisation (burn_scrypt_cost below) is unchanged.
        enforce_auth_rate_limit(self.backend, handler, "signin", email=email,
                                limits=self.auth_rate_limits)
        # Look up the tenant for this email. When the same email exists in more
        # than one tenant (possible via org-invite provisioning), pick the
        # NEWEST tenant — the same rule web/app.py uses — so both surfaces
        # resolve the same tenant for the same email.
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT tenant_id FROM cloud_identity_accounts "
                "WHERE email = ? ORDER BY created_at DESC LIMIT 1",
                (email,),
            ).fetchone()
        if row is None:
            # Timing parity: an unknown email must cost the same scrypt work
            # as a wrong password on a known email, or signin becomes a
            # user-enumeration timing oracle. Run the dummy computation, then
            # refuse with the IDENTICAL response.
            #
            # The refusal MUST be an AuthError("invalid_credentials") — the
            # exact exception the wrong-password path raises one branch down —
            # so both travel through the same _handle() handler and produce
            # byte-identical bodies. A _ServiceError here rendered a different
            # message string ("Invalid email or password") than the AuthError
            # path ("invalid_credentials"): statuses matched, so every
            # status-only test passed, while the body text let anyone
            # enumerate registered email addresses. A caller still gets a
            # usable error; the code and message are simply identical for a
            # known and an unknown account at the same point in the sequence.
            self.accounts.burn_scrypt_cost(password)
            raise AuthError("invalid_credentials")
        tenant_id = row["tenant_id"]
        account_id = self.accounts.authenticate(self.backend, tenant_id, email, password)
        # A password remains valid for account recovery, but it cannot mint a
        # tenant session after the account has been removed from that tenant.
        with self.backend.transaction() as tx:
            member_row = tx.execute(
                "SELECT role FROM cloud_identity_members WHERE tenant_id = ? AND account_id = ?",
                (tenant_id, account_id),
            ).fetchone()
        if member_row is None:
            raise AuthError("invalid_credentials")
        role = member_row["role"]
        session_id, session_token = self.sessions.create(
            self.backend, tenant_id, account_id, role
        )
        return _json_response(HTTPStatus.OK, {
            "account_id": account_id,
            "tenant_id": tenant_id,
            "session_token": session_token,
            "role": role,
        })

    def handle_refresh(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        """Rotate one live hosted session into a fresh one-time bearer.

        Refresh is deliberately session-only: agent keys are the stable
        connector credential and must never become refreshable through this
        endpoint. The body is consumed before authentication so a rejected
        request cannot leave unread bytes on a persistent HTTP connection.
        """
        _read_body(handler)
        token = _bearer_token(handler)
        self._authenticate_session(handler)
        enforce_auth_rate_limit(
            self.backend, handler, "refresh", limits=self.auth_rate_limits,
        )
        _session_id, new_token = self.sessions.rotate(self.backend, token or "")
        return _json_response(HTTPStatus.OK, {
            "session_token": new_token,
            "expires_in": DEFAULT_TTL_SECONDS,
        })

    def handle_signout(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        self._authenticate(handler)
        token = _bearer_token(handler)
        if token:
            from weft_cloud.identity.tokens import hash_token
            token_hash = hash_token(token)
            if token.startswith("agk_"):
                # An agent-key bearer IS the key: signout must kill the key
                # itself, or it would answer signed_out:true while the
                # presented credential stays live (the silent-failure form of
                # the signout regression). One transaction: revoke_by_token_hash
                # does the lookup, the UPDATE, and the room-seat release under a
                # single writer lock. Calling it from inside a nested transaction
                # would BEGIN IMMEDIATE on a SECOND connection — SQLite has one
                # writer, so that inner BEGIN blocks until this one commits, and
                # this one cannot commit until the inner returns: a 15s
                # self-deadlock surfacing as a 500.
                self.agent_keys.revoke_by_token_hash(self.backend, token_hash)
            else:
                self.sessions.revoke_by_token_hash(self.backend, token_hash)
        return _json_response(HTTPStatus.OK, {"signed_out": True})

    def handle_me(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        account = self.accounts.get_by_id(self.backend, ctx.account_id)
        return _json_response(HTTPStatus.OK, {
            "account_id": ctx.account_id,
            "tenant_id": ctx.tenant_id,
            "role": ctx.role,
            "email": account["email"] if account else None,
            "agent_id": ctx.agent_id,
        })

    # ------------------------------------------------------------------
    # Org endpoints
    # ------------------------------------------------------------------

    def handle_list_members(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        members = self.orgs.list_members(ctx)
        return _json_response(HTTPStatus.OK, {"members": members})

    def handle_invite(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        email = body.get("email")
        role = body.get("role", "member")
        if not isinstance(email, str) or not email:
            raise _ServiceError("invalid_argument", "email is required")
        if not isinstance(role, str):
            raise _ServiceError("invalid_argument", "role must be a string")
        try:
            email = validate_email(email)
        except ValueError as exc:
            raise _ServiceError("invalid_argument", str(exc)) from exc
        invite_id, _raw_token = self.invites.create(ctx, email, role)
        # ``invites.create`` has durably handed the message to the configured
        # outbox before returning.  The raw fiv_ token remains exclusively in
        # that delivery flow; it is never a bearer credential in this admin
        # response.
        return _json_response(HTTPStatus.CREATED, {
            "invite_id": invite_id,
            "email": email,
            "role": role,
            "delivery_status": "queued",
        })

    def handle_accept_invite(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        body = _read_body(handler)
        token = body.get("invite_token")
        email = body.get("email")
        password = body.get("password")
        if (
            not isinstance(token, str)
            or not isinstance(email, str)
            or not isinstance(password, str)
            or not token
            or not email
            or not password
        ):
            raise _ServiceError("invalid_argument",
                                "invite_token, email, and password are required")
        try:
            email = validate_email(email)
        except ValueError as exc:
            raise _ServiceError("invalid_argument", str(exc)) from exc
        try:
            validate_password(password)
        except ValueError as exc:
            raise _ServiceError("invalid_argument", str(exc)) from exc
        account_id, session_token = self.invites.accept(self.backend, token, email, password)
        return _json_response(HTTPStatus.OK, {
            "account_id": account_id,
            "session_token": session_token,
        })

    # ------------------------------------------------------------------
    # Room endpoints
    # ------------------------------------------------------------------

    def _reject_identity_args(self, body: Mapping[str, Any], *,
                              allow_self_owner: bool = False) -> None:
        """Refuse any client-supplied identity argument.

        Identity is derived from the authenticated session (``ctx``) and NEVER
        from the request body — accepting ``agent_id`` / ``sender_agent_id`` /
        ``owner_agent_id`` and friends was the live cross-tenant impersonation
        vulnerability on this service.         ``allow_self_owner`` lets the room
        create/connect endpoints accept an ``owner_agent_id`` that names the
        caller's account (redundant, but harmless); the handler still verifies
        it equals ``ctx.account_id``.
        """
        supplied = sorted(set(body) & _FORBIDDEN_IDENTITY_ARGS)
        if not supplied:
            return
        if allow_self_owner and set(supplied) <= {"owner_agent_id"}:
            return
        raise _ServiceError(
            "invalid_argument",
            "Identity is derived from your authenticated session; argument(s) "
            f"{', '.join(supplied)} are not accepted",
        )

    def _room_tenant(self, room_id: str, agent_id: str) -> str:
        """Resolve the effective tenant for a room operation via membership.

        Agents who joined via a cross-tenant link have their membership in
        the room's owning tenant, not their own. Resolution goes through the
        membership row keyed on ``agent_id`` — the caller's authenticated
        account or distinct agent-key identity, established by the credential
        and never by the request body — so a caller can only ever resolve a
        room they are an active member of.
        Anything else yields the uniform ``room_not_found`` — identical to a
        fabricated room_id, so the endpoint is not a room-existence oracle.
        A non-string container ``room_id`` (list/dict) is refused here as the
        caller's ``invalid_argument`` 400 BEFORE any DB bind — it previously
        raised ``sqlite3.ProgrammingError`` on the bind and escaped as a 500.
        """
        if not isinstance(room_id, str) or not room_id.strip():
            raise _ServiceError("invalid_argument", "room_id must be a non-empty string")
        try:
            with self.backend.transaction() as tx:
                return self.rooms._resolve_room_tenant(tx, room_id, agent_id)
        except RoomError:
            raise _ServiceError("room_not_found", "Room not found", HTTPStatus.NOT_FOUND)

    def handle_create_room(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        ctx.require_role("admin")
        body = _read_body(handler)
        self._reject_identity_args(body, allow_self_owner=True)
        owner_agent_id = body.get("owner_agent_id", ctx.account_id)
        if owner_agent_id != ctx.account_id:
            raise _ServiceError(
                "invalid_argument",
                "Identity is derived from your authenticated session; "
                "owner_agent_id must be your account identity",
            )
        cap = body.get("cap", DEFAULT_ROOM_CAP)
        name = body.get("name")
        ttl_seconds = body.get("ttl_seconds", 86400)
        # The actor token is derived from the session — recorded as an
        # informational hash only; room authorization is bound to the account.
        actor_token = _bearer_token(handler)
        result = self.rooms.create_room(
            ctx.tenant_id, owner_agent_id, actor_token, cap=cap,
            name=name, ttl_seconds=ttl_seconds, origin=self.origin,
            actor_account_id=ctx.account_id,
        )
        # Audit.
        self.backend.append_audit(
            ctx.tenant_id, "room.create", ctx.account_id, result["room_id"],
            json.dumps({"cap": cap}),
        )
        return _json_response(HTTPStatus.CREATED, result)

    def handle_connect_room(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        """High-level "connect me to another agent" affordance (POST /v1/rooms/connect).

        Collapses the common case — create a room AND return the self-describing
        shareable URL in one call, so an LLM that hears "connect me to Claude",
        "link", "introduce", "bring in", or "talk to another agent or assistant"
        has ONE tool that produces the exact string to hand over.

        Give the returned ``shareable_link`` to the other agent: it is a URL the
        other agent can open to discover the join endpoint, auth scheme, and
        protocol without any further tribal knowledge.

        What this tool does NOT do: it does not deliver the URL to the other
        agent. Delivery is the caller's job — a human pastes it, or the calling
        agent uses its own channel. This is additive sugar over /v1/rooms/create
        and reuses CloudRoomService.create_room; it is not a replacement for it.
        """
        ctx = self._authenticate(handler)
        ctx.require_role("admin")
        body = _read_body(handler)
        self._reject_identity_args(body, allow_self_owner=True)
        owner_agent_id = body.get("owner_agent_id", ctx.account_id)
        if owner_agent_id != ctx.account_id:
            raise _ServiceError(
                "invalid_argument",
                "Identity is derived from your authenticated session; "
                "owner_agent_id must be your account identity",
            )
        cap = body.get("cap", DEFAULT_ROOM_CAP)
        name = body.get("name")
        ttl_seconds = body.get("ttl_seconds", 86400)
        actor_token = _bearer_token(handler)
        result = self.rooms.create_room(
            ctx.tenant_id, owner_agent_id, actor_token, cap=cap,
            name=name, ttl_seconds=ttl_seconds, origin=self.origin,
            actor_account_id=ctx.account_id,
        )
        self.backend.append_audit(
            ctx.tenant_id, "room.connect", ctx.account_id, result["room_id"],
            json.dumps({"cap": cap}),
        )
        return _json_response(HTTPStatus.CREATED, {
            "room_id": result["room_id"],
            "link_token": result["link_token"],
            "shareable_link": result["shareable_link"],
            "expires_at": result["expires_at"],
            "cap": result["cap"],
            "state": result["state"],
        })

    def handle_join_descriptor(self, handler: BaseHTTPRequestHandler, link_token: str) -> None:
        """Unauthenticated description of a shareable link (GET /j/<token>).

        Serves two audiences from ONE URL:
          - a machine (``Accept: application/json``) gets a JSON join-descriptor
            naming the origin, room_id, the POST endpoint, the auth scheme, and
            the agent-card pointer;
          - a human (``Accept: text/html``) gets a readable page explaining what
            the link is and how to connect, reusing the connect-page copy.

        This is a PURE READ: it never joins the room and never consumes or
        invalidates the token — joining stays an explicit authenticated POST to
        /v1/rooms/join. It reveals only what a joining agent strictly needs:
        the room_id and the join contract. No org identity, member emails, or
        event log are exposed. Malformed and unknown tokens return the SAME 404
        shape so the endpoint is not an oracle.
        """
        room_id = self.rooms.resolve_room_by_link_token(link_token)
        accept = handler.headers.get("Accept", "") or ""
        wants_json = "application/json" in accept or "application/*" in accept
        if room_id is None:
            if wants_json:
                handler._send_json(HTTPStatus.NOT_FOUND,
                                   {"error": {"code": "not_found", "message": "Not found"}})
            else:
                handler.send_response(HTTPStatus.NOT_FOUND)
                body = _html_page("Not found", "<p>This link does not open a room.</p>")
                handler.send_header("Content-Type", "text/html; charset=utf-8")
                handler.send_header("Content-Length", str(len(body)))
                handler.send_header("Cache-Control", "no-store")
                handler._send_security_headers(html=True)
                handler.end_headers()
                handler.wfile.write(body)
            return

        if wants_json:
            descriptor = {
                "service": "weft",
                "profile": {"name": "weft.a2a", "version": "2.0"},
                "origin": self.origin,
                "room_id": room_id,
                "join": {
                    "endpoint": f"{self.origin}/v1/rooms/join",
                    "method": "POST",
                    "auth_scheme": "bearer-session-or-agent-key",
                    "request": {
                        "room_id": room_id,
                        "link_token": link_token,
                        "consent": True,
                        "capabilities": [],
                    },
                },
                "agent_card": f"{self.origin}/.well-known/agent-card.json",
                "notes": {
                    "token_source": "The path segment of this URL IS the link_token the join endpoint consumes.",
                    "consent": "Joining requires explicit consent (literal boolean true) and an authenticated session from signup/signin.",
                    "not_delivered": "Opening this URL never joins the room. Join only happens through an explicit authenticated POST.",
                },
            }
            body = json.dumps(descriptor, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Cache-Control", "no-store")
            handler._send_security_headers()
            handler.end_headers()
            handler.wfile.write(body)
            return

        # Human audience — reuse the connect-page copy.
        from weft_cloud.web.copy import connect_page_body
        body_html = (
            f'<p>This link opens a Weft room. Give it to the agent you want to '
            f'connect, or use the config below yourself. The link is <code>{_html_esc(self.origin + "/j/" + link_token)}</code>.</p>'
            + connect_page_body(room_id, link_token)
        )
        body = _html_page("Connect an agent", body_html)
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler._send_security_headers(html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def handle_agent_card(self, handler: BaseHTTPRequestHandler) -> None:
        """Unauthenticated machine-readable agent card (GET /.well-known/agent-card.json).

        Lets an agent that knows only our origin discover what the service is,
        which protocol/profile it speaks, and how to join. Cache-friendly. No
        secrets, no per-tenant data, no member or org identity.

        The card names OUR OWN profile (``weft.a2a``) and explicitly does
        NOT claim conformance with the A2A Protocol — that is a proprietary
        repository namespace, and the card must not imply certification it does
        not have.
        """
        card = {
            "service": "weft",
            "description": "Weft is a coordination layer that connects independent AI agents into a shared, consent-gated room.",
            "profile": {"name": "weft.a2a", "version": "2.0"},
            "mcp_protocol": "2025-11-25",
            "origin": self.origin,
            "join_endpoint": f"{self.origin}/v1/rooms/join",
            "join_method": "POST",
            "auth_scheme": "bearer-session-or-agent-key",
            "link_format": f"{self.origin}/j/<link_token>",
            "connection_tiers": [
                {"tier": "mcp-stdio",
                 "description": "A Weft MCP coordinator exposing room_* tools over stdio."},
                {"tier": "streamable-http",
                 "description": "The hosted JSON-RPC-over-HTTP surface (this service)."},
                {"tier": "bridge-webhook",
                 "description": "Signed webhook delivery for non-MCP hosts."},
                {"tier": "sdk",
                 "description": "weft_sdk, a stdlib-only Python client."},
            ],
            "notes": {
                "profile_scope": "weft.a2a is a proprietary profile namespace used by the Weft service. It is not a ratified protocol standard.",
                "a2a_shape": "The join model (one link, many agents, per-agent capability lists) is conceptually aligned with agent-to-agent ideas; this is a factual description of the layout.",
            },
        }
        body = json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "public, max-age=3600")
        handler._send_security_headers()
        handler.end_headers()
        handler.wfile.write(body)

    def handle_join_room(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        link_token = body.get("link_token")
        consent = body.get("consent", False)
        capabilities = body.get("capabilities", [])
        actor_token = _bearer_token(handler)
        if not room_id or not link_token:
            raise _ServiceError("invalid_argument", "room_id and link_token are required")
        result = self.rooms.join_room(
            ctx.tenant_id, room_id, link_token, ctx.agent_id, consent, actor_token, capabilities,
        )
        self.backend.append_audit(
            ctx.tenant_id, "room.join", ctx.account_id, room_id,
            json.dumps({"agent_id": ctx.agent_id}),
        )
        return _json_response(HTTPStatus.OK, result)

    def handle_room_info(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        params = {}
        if "?" in handler.path:
            qs = handler.path.split("?", 1)[1]
            for pair in qs.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = v
        room_id = params.get("room_id")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id query parameter is required")
        # A query-param agent_id is IGNORED, never honoured: the caller
        # resolves as their OWN authenticated identity.
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        result = self.rooms.room_info(
            tenant_id, room_id, ctx.agent_id, owner_agent_id=ctx.account_id,
        )
        return _json_response(HTTPStatus.OK, result)

    def handle_room_poll(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        after_seq = body.get("after_seq")
        limit = body.get("limit", 100)
        message_kinds = body.get("message_kinds")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        result = self.rooms.poll(tenant_id, room_id, ctx.agent_id, after_seq, limit,
                                 message_kinds=message_kinds)
        return _json_response(HTTPStatus.OK, result)

    def handle_room_wait(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        """Blocking long-poll (POST /v1/rooms/wait) — REST parity with MCP ``room_wait``.

        Shares the SAME waiting implementation the hosted MCP tool calls:
        ``CloudRoomService.wait`` — one code path, two surfaces. No waiting
        logic is forked here; this handler is only the /v1 framing around it
        (auth, identity, tenant resolution, concurrency bound, JSON).

        Identity is derived EXCLUSIVELY from the authenticated session. Any
        identity argument smuggled in the body (``agent_id``, ``sender_agent_id``,
        ``caller_agent_id``, ``tenant_id``, and friends) is rejected — that exact
        pattern was a live cross-tenant impersonation vulnerability on this
        service, and it is refused here exactly as the MCP surface refuses it.

        A timeout is a NORMAL outcome: the empty poll result comes back as a
        200 with ``timed_out: true``, never an error, so a caller simply loops.
        A non-member resolves the uniform ``room_not_found`` 404 BEFORE any
        blocking starts — byte-identical for a real room and a fabricated one,
        so the endpoint is not a room-existence oracle.
        """
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        # Same concurrency bound as the MCP surface: a blocking long-poll holds
        # one HTTP connection and one worker thread, so the shared semaphore
        # caps combined in-flight waiters and refuses FAST when exhausted.
        if not _wait_slots.acquire(blocking=False):
            raise _ServiceError(
                "wait_busy",
                "Too many room_wait calls are in flight; retry with room_poll or retry room_wait shortly",
                HTTPStatus.TOO_MANY_REQUESTS,
            )
        try:
            tenant_id = self._room_tenant(room_id, ctx.agent_id)
            result = self.rooms.wait(
                tenant_id, room_id, ctx.agent_id,
                after_seq=body.get("after_seq"),
                timeout_seconds=body.get("timeout_seconds", 20),
                limit=body.get("limit", 100),
                message_kinds=body.get("message_kinds"),
            )
        finally:
            _wait_slots.release()
        return _json_response(HTTPStatus.OK, result)

    def handle_room_ack(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        seq = body.get("seq")
        if not room_id or seq is None:
            raise _ServiceError("invalid_argument", "room_id and seq are required")
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        result = self.rooms.ack(tenant_id, room_id, ctx.agent_id, seq)
        return _json_response(HTTPStatus.OK, result)

    def handle_room_send(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        target_spec = body.get("target_spec", "*")
        payload = body.get("payload", {})
        exclude_sender = body.get("exclude_sender", True)
        message_kind = body.get("message_kind")
        idempotency_key = body.get("idempotency_key")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        result = self.rooms.room_send(
            tenant_id, room_id, ctx.agent_id, target_spec, payload, exclude_sender,
            message_kind=message_kind, idempotency_key=idempotency_key,
        )
        return _json_response(HTTPStatus.OK, result)

    def handle_room_receipts(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        """Query delivery/read state for the caller's own sends (POST /v1/rooms/receipts).

        REST parity with the hosted MCP tool ``room_receipts``: same
        CloudRoomService.receipts call, same tenant resolution via the
        caller's membership, and the same sender-scoped result — unknown or
        non-owned entry ids return ``not_found`` inside the receipts list
        without revealing another sender's outbox state.
        """
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        entry_ids = body.get("entry_ids")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        if entry_ids is None:
            raise _ServiceError("invalid_argument", "entry_ids is required")
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        result = self.rooms.receipts(tenant_id, room_id, ctx.agent_id, entry_ids)
        return _json_response(HTTPStatus.OK, result)

    def handle_room_leave(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        result = self.rooms.leave_room(tenant_id, room_id, ctx.agent_id)
        return _json_response(HTTPStatus.OK, result)

    def handle_room_remove_member(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        """Owner-only removal of a member (POST /v1/rooms/remove_member).

        REST parity with the hosted MCP tool ``room_remove_member``: the
        owner frees a member's seat exactly as over MCP. The removed member is
        refused on its very next request; removal is NOT a ban — a removed
        member who still holds a valid link can rejoin.
        """
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        member_id = body.get("member_id")
        if not room_id or not member_id:
            raise _ServiceError("invalid_argument", "room_id and member_id are required")
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        result = self.rooms.remove_member(
            tenant_id, room_id, ctx.account_id, member_id,
            caller_agent_id=ctx.agent_id,
        )
        self.backend.append_audit(
            tenant_id, "room.remove_member", ctx.account_id, room_id,
            json.dumps({"member_id": member_id}),
        )
        return _json_response(HTTPStatus.OK, result)

    def handle_room_close(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        result = self.rooms.close_room(
            tenant_id, room_id, ctx.agent_id, owner_agent_id=ctx.account_id,
        )
        return _json_response(HTTPStatus.OK, result)

    def handle_revoke_link(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        link_id = body.get("link_id")
        if not room_id or not link_id:
            raise _ServiceError("invalid_argument", "room_id and link_id are required")
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        result = self.rooms.revoke_link(
            tenant_id, room_id, ctx.account_id, link_id,
            caller_agent_id=ctx.agent_id,
        )
        return _json_response(HTTPStatus.OK, result)

    def handle_list_rooms(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        # The listing is scoped to the CALLER's own identity, never to a
        # client-chosen ``agent_id``. Accepting ?agent_id=<victim> let anyone
        # enumerate every room of any agent in every tenant — the pivot that
        # turns one impersonated room into everything (and the source of the
        # tenant_id needed for the signup takeover). The authenticated identity
        # is the only identity this listing trusts.
        rooms = self._list_rooms_any_tenant(ctx.agent_id)
        return _json_response(HTTPStatus.OK, {"rooms": rooms})

    def _list_rooms_any_tenant(self, agent_id: str) -> list[dict]:
        """List rooms for a member across all tenants (cross-tenant support)."""
        with self.backend.transaction() as tx:
            rows = tx.execute(
                "SELECT r.room_id, r.tenant_id, r.name, r.state, r.cap, r.owner_agent_id, r.created_at "
                "FROM cloud_rooms r "
                "JOIN cloud_room_members m ON m.room_id = r.room_id AND m.tenant_id = r.tenant_id "
                "WHERE m.agent_id = ? AND m.status = 'active' "
                "ORDER BY r.created_at",
                (agent_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def handle_event_log(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        events = self.rooms.event_log(tenant_id, room_id, ctx.agent_id)
        return _json_response(HTTPStatus.OK, {"events": events})

    def handle_room_heartbeat(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        result = self.rooms.heartbeat(tenant_id, room_id, ctx.agent_id)
        return _json_response(HTTPStatus.OK, result)

    def handle_groups(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        group_name = body.get("group_name")
        action = body.get("action", "list")
        members = body.get("members")
        if not room_id or not group_name:
            raise _ServiceError("invalid_argument", "room_id and group_name are required")
        tenant_id = self._room_tenant(room_id, ctx.agent_id)
        result = self.rooms.groups(tenant_id, room_id, ctx.agent_id, group_name, action, members)
        return _json_response(HTTPStatus.OK, result)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


def _accept_ranges(accept_header: str | None) -> list[tuple[str, float]]:
    """Parse an Accept header into ``[(media-range, q)]`` honouring q-parameters.

    A missing ``q`` defaults to 1.0; ``q=0`` (or an unparseable q) is treated
    as "not acceptable". Media ranges are returned in header order.
    """
    if not accept_header:
        return []
    ranges: list[tuple[str, float]] = []
    for part in accept_header.split(","):
        media, _, params = part.partition(";")
        media = media.strip().lower()
        if not media:
            continue
        q = 1.0
        for param in params.split(";"):
            param = param.strip()
            if param[:2] == "q=":
                try:
                    q = float(param[2:].strip())
                except ValueError:
                    q = 0.0
                q = 0.0 if q <= 0 else q
        ranges.append((media, q))
    return ranges


def _accept_quality(ranges: list[tuple[str, float]], media_type: str) -> float:
    """Effective q for ``media_type`` under RFC 7231 specificity ordering.

    An exact media range beats ``type/*`` which beats ``*/*``, so an explicit
    ``q=0`` for a concrete type excludes it even when ``*/*`` is also present.
    """
    mtype, msub = media_type.split("/", 1)
    best: tuple[int, float] | None = None
    for media, q in ranges:
        if media == "*/*":
            spec, rq = 0, q
        elif media == f"{mtype}/*":
            spec, rq = 1, q
        elif media == media_type:
            spec, rq = 2, q
        else:
            continue
        if best is None or spec > best[0]:
            best = (spec, rq)
    return best[1] if best is not None else 0.0


def _negotiate_mcp_media(accept_header: str | None) -> tuple[bool, bool]:
    """Return ``(accepts_json, accepts_sse)`` for the hosted /mcp endpoint.

    A request with no Accept header keeps the long-standing default: JSON
    only. ``q=0`` ranges and unmatched media types yield False; a wildcard
    accepts both.
    """
    ranges = _accept_ranges(accept_header)
    if not ranges:
        return True, False
    accepts_json = _accept_quality(ranges, "application/json") > 0
    accepts_sse = _accept_quality(ranges, "text/event-stream") > 0
    return accepts_json, accepts_sse


def _sse_data_frame(payload: dict) -> bytes:
    """Frame a JSON-RPC payload as one SSE ``data:`` event.

    JSON strings escape embedded newlines, so the frame is normally a single
    line; the defensive split keeps the frame spec-correct even if that ever
    changes.
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"data: " + body.replace(b"\n", b"\ndata: ") + b"\n\n"


class _CloudHTTPHandler(BaseHTTPRequestHandler):
    """Routes HTTP requests to the service.

    Same idiom as ``weft_mcp/server.py``'s ``_MCPRequestHandler``:
    ``BaseHTTPRequestHandler`` with a class-level ``service`` reference.
    """

    service: WeftCloudService

    server_version = "weft-cloud/0.1.0"

    # Keepalive interval for SSE long-polls (POST /mcp, room_wait). 12s is
    # deliberately below the 20-30s idle floors of common proxies, load
    # balancers and client read timeouts, yet far above the wait loop's 0.25s
    # poll cadence, so a blocked stream is visibly alive without being noisy.
    # A 20s (default) wait sees one keepalive before the final frame; a 30s
    # (max) wait sees two.
    mcp_sse_keepalive_seconds = 12

    def log_message(self, format: str, *args: Any) -> None:
        # Never log request bodies or tokens.
        return

    def _send_security_headers(self, *, html: bool = False) -> None:
        """Emit the shared security header block (web/security_headers.py).

        ``html=True`` also sends the Content-Security-Policy; CSP is scoped to
        HTML responses on purpose. JSON/SSE responses get the always-on set
        (HSTS, nosniff, Referrer-Policy, X-Frame-Options, Permissions-Policy).
        """
        for name, value in security_headers(html=html):
            self.send_header(name, value)

    def _send_json(self, status: int, payload: dict[str, Any],
                   extra_headers: Mapping[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.service.metrics.inc("weft_http_responses_total", {"status": str(status)})
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self._send_security_headers()
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, body: str,
                   content_type: str = "text/plain; version=0.0.4") -> None:
        encoded = body.encode("utf-8")
        self.service.metrics.inc("weft_http_responses_total", {"status": str(status)})
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def _send_sse_headers(self) -> None:
        """Open a streaming 200 ``text/event-stream`` response.

        The body carries no Content-Length: it is delimited by connection
        close (``close_connection`` is forced True after the last frame), so a
        client reading the body sees EOF exactly when the stream ends.
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._send_security_headers()
        self.end_headers()

    def _send_sse_comment(self, comment: bytes) -> None:
        """Write a comment frame (``: ...``), which SSE clients ignore.

        ``wfile`` is an unbuffered ``_SocketWriter``, so the write reaches the
        socket immediately; ``flush`` is still called for explicitness.
        """
        self.wfile.write(comment)
        self.wfile.flush()

    def _send_sse_event(self, payload: dict) -> None:
        """Write the final ``data:`` frame for a JSON-RPC response and close.

        The response has no Content-Length, so end-of-body is signalled by
        closing the connection after the frame.
        """
        self.wfile.write(_sse_data_frame(payload))
        self.wfile.flush()
        self.close_connection = True

    def _make_sse_keepalive(self) -> Callable[[], None]:
        """Build the keepalive callback passed to a blocking tool dispatch.

        The callback rate-limits itself to ``mcp_sse_keepalive_seconds`` and
        is invoked by the wait loop while ``room_wait`` is blocked; the first
        pulse fires immediately so the client sees the stream is live the
        moment blocking starts.
        """
        interval = self.mcp_sse_keepalive_seconds
        last = [time.monotonic() - interval]

        def _pulse() -> None:
            now = time.monotonic()
            if now - last[0] >= interval:
                last[0] = now
                self._send_sse_comment(b": keepalive\n\n")

        return _pulse

    def _send_rate_limited(self, retry_after: int) -> None:
        retry_after = max(1, int(retry_after))
        self._send_json(
            HTTPStatus.TOO_MANY_REQUESTS,
            {"error": {
                "code": "rate_limited",
                "message": f"Rate limit exceeded. Retry after {retry_after} seconds.",
            }},
            {"Retry-After": str(retry_after)},
        )

    def _discard_mcp_body(self) -> None:
        """Drain a bounded request body before closing a rejected connection."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        remaining = min(max(length, 0), MAX_JSON_RPC_BYTES)
        while remaining:
            chunk = self.rfile.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _mcp_identity_limiter_key(self) -> str:
        """Return a fairness key derived from authenticated identity when possible."""
        token = _bearer_token(self)
        if token:
            try:
                ctx = self.service.resolve_identity(token)
            except AuthError:
                pass
            else:
                return f"mcp:tenant:{ctx.tenant_id}:agent:{ctx.agent_id}"
        # Invalid/unauthenticated requests have no trusted identity. Keep
        # those on the source-IP circuit breaker rather than creating a
        # user-controlled key from an untrusted header or body field.
        return f"mcp:ip:{self.client_address[0]}"

    def _handle_mcp_post(self) -> None:
        """Serve the authenticated, tenant-confined MCP endpoint at POST /mcp.

        Mirrors the coordinator's Streamable HTTP framing (``Mcp-Method`` /
        ``Mcp-Name`` header checks, JSON-RPC notifications -> 202) but every
        request must authenticate against the CLOUD identity plane. Auth is
        checked before any method is dispatched, so an unauthenticated
        initialize / tools/list / tools/call is refused alike and reveals
        nothing about the tool set or the store.

        Transport (Streamable HTTP): the request's Accept header is honoured.

          - ``text/event-stream`` ONLY   -> the response is an SSE stream: a
            ``data: <json>`` frame, with ``: keepalive`` comment frames while
            a blocking call (``room_wait``) holds the connection open.
          - ``application/json`` (alone or alongside SSE) -> the long-standing
            plain-JSON response. Picking JSON whenever the client accepts it
            keeps every currently-working caller byte-identical to today; SSE
            is only used when the client explicitly excludes JSON.
          - accepts neither -> 406.

        A failed authentication is ALWAYS the same byte-identical JSON 401,
        whatever media type the client asked for — auth outranks negotiation.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_JSON_RPC_BYTES:
            self.close_connection = True
            self._discard_mcp_body()
            error = (_request_too_large_error()
                     if length > MAX_JSON_RPC_BYTES
                     else _json_rpc_error(None, -32600, "Invalid request size"))
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error)
            return
        try:
            request = _strict_json_loads(self.rfile.read(length))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, _json_rpc_error(None, -32700, "Parse error"))
            return
        method = request.get("method") if isinstance(request, dict) else None
        request_id = request.get("id") if isinstance(request, dict) else None
        header_method = self.headers.get("Mcp-Method")
        header_name = self.headers.get("Mcp-Name")
        if header_method and header_method != method:
            self._send_json(HTTPStatus.BAD_REQUEST, _json_rpc_error(
                request_id, -32600, "Mcp-Method does not match the JSON-RPC method"))
            return
        params = request.get("params") if isinstance(request, dict) else None
        method_name = params.get("name") if isinstance(params, dict) else None
        if method == "tools/call" and header_name and header_name != method_name:
            self._send_json(HTTPStatus.BAD_REQUEST, _json_rpc_error(
                request_id, -32600, "Mcp-Name does not match params.name"))
            return
        dispatcher = HostedMCPDispatcher(self.service)
        bearer = _bearer_token(self)
        try:
            pre = dispatcher.preauthenticate(request, bearer)
        except HostedMCPAuthError:
            # Refused identically under every negotiated media type.
            self._send_json(HTTPStatus.UNAUTHORIZED, _json_rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32001, "Unauthorized"))
            return
        if pre is None:
            # Byte-identical shape refusal to handle_json_rpc: id=None for a
            # non-2.0 envelope, the request id for a bad method/params.
            bad_id = request.get("id") if (isinstance(request, dict)
                                           and request.get("jsonrpc") == "2.0") else None
            self._send_json(HTTPStatus.OK, _json_rpc_error(bad_id, -32600,
                                                           "Invalid JSON-RPC request"))
            return
        request_id, _method, _params, is_notification, ctx = pre
        accepts_json, accepts_sse = _negotiate_mcp_media(self.headers.get("Accept"))
        if not accepts_json and not accepts_sse:
            self._send_json(HTTPStatus.NOT_ACCEPTABLE, _json_rpc_error(
                request_id, -32600,
                "Not acceptable: this endpoint produces application/json or text/event-stream"))
            return
        # A notification (or a notifications/* method carrying an id) is
        # answered 202 with no body on the JSON path; keep that exact shape on
        # the SSE path instead of opening a stream that has no payload.
        replies_202 = is_notification or method in _MCP_NOTIFICATION_METHODS
        if accepts_sse and not accepts_json and not replies_202:
            # Streamable HTTP SSE response. Headers go out first so keepalive
            # comment frames can be written while a tool (room_wait) blocks;
            # the JSON-RPC payload is delivered as the final data: frame.
            self._send_sse_headers()
            response = dispatcher.handle_json_rpc(
                request, bearer, ctx=ctx, keepalive=self._make_sse_keepalive())
            self._send_sse_event(response)
            return
        response = dispatcher.handle_json_rpc(request, bearer, ctx=ctx)
        if response is None:
            self.send_response(HTTPStatus.ACCEPTED)
            self.send_header("Content-Length", "0")
            self._send_security_headers()
            self.end_headers()
            return
        self._send_json(HTTPStatus.OK, response)

    def _handle(self, method: str, handler_fn) -> None:
        try:
            status, body = handler_fn(self)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)
        except _ServiceError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except RoomError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except QuotaError as exc:
            # Plan limit hit. Include the caller's OWN limit + plan so the
            # error is actionable; never another tenant's data or an internal id.
            error = {"code": exc.code, "message": str(exc)}
            if exc.limit_name is not None:
                error["limit"] = {
                    "name": exc.limit_name,
                    "value": exc.limit_value,
                    "plan": exc.plan_id,
                }
            self._send_json(HTTPStatus.CONFLICT, {"error": error})
        except RateLimitedError as exc:
            retry_after = str(int(max(1.0, exc.retry_after or 1.0)))
            body = json.dumps({
                "error": {"code": exc.code, "message": str(exc),
                          "retry_after": float(exc.retry_after or 1.0)},
            })
            self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", retry_after)
            self.send_header("Cache-Control", "no-store")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        except AuthError as exc:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": {"code": exc.code, "message": str(exc)}})
        except RoleError as exc:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": {"code": exc.code, "message": str(exc)}})
        except Exception as exc:
            # Never leak internals.
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": {"code": "internal_error", "message": "Internal server error"}})

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path in {"/healthz", "/health"}:
            self._send_json(HTTPStatus.OK, {"status": "ok", "service": "weft-cloud"})
            return
        if path in {"/v1/healthz"}:
            self._send_json(HTTPStatus.OK, {"status": "ok", "service": "weft-cloud"})
            return
        if path in {"/readyz", "/v1/readyz"}:
            try:
                self._send_json(HTTPStatus.OK, self.service.readiness_status())
            except Exception:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"status": "unavailable", "service": "weft-cloud"},
                )
            return
        if path in {"/metrics", "/v1/metrics"}:
            configured = self.service.metrics_token
            if configured is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": {"code": "not_found", "message": "Not found"}},
                )
                return
            supplied = _bearer_token(self)
            if supplied is None or not hmac.compare_digest(supplied, configured):
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": {"code": "unauthorized", "message": "Metrics require authorization"}},
                )
                return
            self._send_text(HTTPStatus.OK, self.service.metrics.render())
            return
        if path == "/.well-known/agent-card.json":
            self.service.handle_agent_card(self)
            return
        join_match = _JOIN_LINK_RE.match(path)
        if join_match:
            self.service.handle_join_descriptor(self, join_match.group(1))
            return
        if path == "/mcp":
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED,
                            {"error": "Weft hosted MCP GET streaming is not enabled; use POST /mcp"})
            return
        if path in {"/v1/rooms"}:
            self._handle("GET", self.service.handle_list_rooms)
            return
        if path in {"/v1/rooms/info"}:
            self._handle("GET", self.service.handle_room_info)
            return
        if path in {"/v1/me"}:
            self._handle("GET", self.service.handle_me)
            return
        if path in {"/v1/org/members"}:
            self._handle("GET", self.service.handle_list_members)
            return
        if path in {"/v1/agent-keys"}:
            self._handle("GET", self.service.handle_list_agent_keys)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "Not found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/mcp":
            limiter = getattr(type(self), "mcp_rate_limiter", None)
            if limiter is None:
                limiter = self.service.mcp_rate_limiter
            ip_limiter = getattr(type(self), "mcp_ip_rate_limiter", None)
            if ip_limiter is None:
                ip_limiter = self.service.mcp_ip_rate_limiter
            ip_key = f"mcp:ip:{self.client_address[0]}"
            ip_allowed, ip_retry_after = ip_limiter.allow(ip_key, reserve=True)
            if not ip_allowed:
                self.close_connection = True
                self._discard_mcp_body()
                self._send_rate_limited(ip_retry_after)
                return
            limiter_key = self._mcp_identity_limiter_key()
            allowed, retry_after = limiter.allow(limiter_key, reserve=True)
            if not allowed:
                ip_limiter.release(ip_key)
                self.close_connection = True
                self._discard_mcp_body()
                self._send_rate_limited(retry_after)
                return
            try:
                self._handle_mcp_post()
            finally:
                limiter.release(limiter_key)
                ip_limiter.release(ip_key)
            return
        routes = {
            "/v1/auth/signup": self.service.handle_signup,
            "/v1/auth/signin": self.service.handle_signin,
            "/v1/auth/refresh": self.service.handle_refresh,
            "/v1/auth/signout": self.service.handle_signout,
            "/v1/rooms/create": self.service.handle_create_room,
            "/v1/rooms/connect": self.service.handle_connect_room,
            "/v1/rooms/join": self.service.handle_join_room,
            "/v1/rooms/leave": self.service.handle_room_leave,
            "/v1/rooms/remove_member": self.service.handle_room_remove_member,
            "/v1/rooms/close": self.service.handle_room_close,
            "/v1/rooms/send": self.service.handle_room_send,
            "/v1/rooms/receipts": self.service.handle_room_receipts,
            "/v1/rooms/poll": self.service.handle_room_poll,
            "/v1/rooms/wait": self.service.handle_room_wait,
            "/v1/rooms/ack": self.service.handle_room_ack,
            "/v1/rooms/heartbeat": self.service.handle_room_heartbeat,
            "/v1/rooms/revoke_link": self.service.handle_revoke_link,
            "/v1/rooms/event_log": self.service.handle_event_log,
            "/v1/rooms/groups": self.service.handle_groups,
            "/v1/org/invite": self.service.handle_invite,
            "/v1/org/accept_invite": self.service.handle_accept_invite,
            "/v1/agent-keys": self.service.handle_create_agent_key,
            "/v1/agent-keys/revoke": self.service.handle_revoke_agent_key,
        }
        handler_fn = routes.get(path)
        if handler_fn is None:
            self._send_json(HTTPStatus.NOT_FOUND,
                            {"error": {"code": "not_found", "message": "Not found"}})
            return
        self._handle("POST", handler_fn)


def create_service(db_path: str = ":memory:", origin: str | None = None) -> WeftCloudService:
    """Create a cloud service backed by a SQLite-WAL database.

    ``db_path=":memory:"`` is used for tests. Pass a file path for persistence.
    ``origin`` is the public base URL used to build shareable links; it defaults
    to ``WEFT_PUBLIC_ORIGIN`` (or the local dev origin) when omitted.
    """
    backend = SqliteWalBackend(db_path)
    backend.initialize()
    return WeftCloudService(backend, origin=origin)


def runtime_config(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve runtime settings for the cloud service launcher.

    Precedence is CLI argv → environment → defaults, so the original
    positional form (``service.py <port> <db-path>``) keeps working while
    containers configure entirely through the environment:

    - ``WEFT_HOST``  (default ``127.0.0.1``)
    - ``WEFT_PORT``  (default ``18788``)
    - ``WEFT_DB_PATH`` (default ``./data/weft-cloud.db``)
    - ``WEFT_PUBLIC_ORIGIN`` (default ``http://127.0.0.1:18788``) — the
      base URL baked into shareable links and served by the /j/<token> and
      agent-card endpoints. No deployment URL is hardcoded.

    A malformed or out-of-range port raises ``ValueError`` so a misconfigured
    deploy fails loudly at startup instead of silently binding the default.
    """
    argv = list(argv) if argv is not None else []
    environ = os.environ if environ is None else environ

    def _port(name: str, raw: str | None, default: int) -> int:
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer, got {raw!r}")
        if not (1 <= value <= 65535):
            raise ValueError(f"{name} must be in 1..65535, got {value}")
        return value

    host = environ.get("WEFT_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = _port("WEFT_PORT", environ.get("WEFT_PORT"), 18788)
    db_path = environ.get("WEFT_DB_PATH", "./data/weft-cloud.db").strip() \
        or "./data/weft-cloud.db"
    origin = environ.get("WEFT_PUBLIC_ORIGIN", "http://127.0.0.1:18788").strip() \
        or "http://127.0.0.1:18788"

    if len(argv) >= 1:
        port = _port("port", argv[0], port)
    if len(argv) >= 2:
        db_path = argv[1]

    return {"host": host, "port": port, "db_path": db_path, "origin": origin}


def serve(host: str = "127.0.0.1", port: int = 18788, db_path: str = "./data/weft-cloud.db",
          origin: str | None = None) -> None:
    """Run the cloud HTTP service (blocking)."""
    service = create_service(db_path, origin=origin)
    _CloudHTTPHandler.service = service
    server = ThreadingHTTPServer((host, port), _CloudHTTPHandler)
    print(f"weft-cloud listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.backend.close()


if __name__ == "__main__":
    import sys
    try:
        cfg = runtime_config(sys.argv[1:])
    except ValueError as exc:
        print(f"weft-cloud: {exc}", file=sys.stderr)
        sys.exit(2)
    serve(cfg["host"], cfg["port"], cfg["db_path"], cfg["origin"])
