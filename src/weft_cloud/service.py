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
via a Bearer session token (``fss_``) except signup/signin. Room mutations
require the actor to be an active member; room reads are member-only.

One link, many agents: the room link (``/r/{room_id}#{token}``) is multi-use
up to the room cap. Any number of distinct agents can redeem it.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http import HTTPStatus
from typing import Any, Mapping
from urllib.parse import urlsplit

from weft_cloud.identity import (
    AccountStore,
    AuthError,
    InviteStore,
    OrgStore,
    RoleError,
    SessionContext,
    SessionStore,
    ensure_identity_schema,
)
from weft_cloud.identity.schema import ensure_schema as _ensure_identity_schema
from weft_cloud.mcp import (
    HostedMCPAuthError,
    HostedMCPDispatcher,
    MAX_JSON_RPC_BYTES,
    _FORBIDDEN_IDENTITY_ARGS,
    _json_rpc_error,
    _wait_slots,
)
from weft_cloud.quotas import QuotaError
from weft_cloud.rate_limit import RateLimitedError
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


def _json_response(status: int, payload: dict[str, Any]) -> tuple[int, bytes]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return status, body


def _read_body(handler: BaseHTTPRequestHandler, max_bytes: int = 1_048_576) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length <= 0 or length > max_bytes:
        raise _ServiceError("invalid_body", "Invalid request size", HTTPStatus.BAD_REQUEST)
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise _ServiceError("invalid_json", "Request body must be JSON", HTTPStatus.BAD_REQUEST)
    if not isinstance(data, dict):
        raise _ServiceError("invalid_body", "Request body must be an object", HTTPStatus.BAD_REQUEST)
    return data


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

    def __init__(self, backend: StorageBackend, origin: str | None = None) -> None:
        self.backend = backend
        self.origin = public_origin(origin)
        self.accounts = AccountStore(backend)
        self.sessions = SessionStore(backend)
        self.orgs = OrgStore(backend)
        self.invites = InviteStore(backend)
        self.rooms = CloudRoomService(backend)
        # Ensure identity + room schema exist.
        _ensure_identity_schema(backend)
        self.rooms._ensure_room_schema()

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _authenticate(self, handler: BaseHTTPRequestHandler) -> SessionContext:
        token = _bearer_token(handler)
        if not token:
            raise _ServiceError("unauthorized", "Authorization Bearer token required",
                                HTTPStatus.UNAUTHORIZED)
        try:
            return self.sessions.validate(self.backend, token)
        except AuthError as exc:
            raise _ServiceError(exc.code, "Invalid session", HTTPStatus.UNAUTHORIZED)

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
        if not email or not password:
            raise _ServiceError("invalid_argument", "email and password are required")
        if not isinstance(email, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise _ServiceError("invalid_argument", "email must be a valid email address")
        if not isinstance(password, str) or len(password) < 8:
            raise _ServiceError("invalid_argument", "password must be at least 8 characters")

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
        if not email or not password:
            raise _ServiceError("invalid_argument", "email and password are required")
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
            # refuse with the identical response.
            self.accounts.burn_scrypt_cost(password)
            raise _ServiceError("invalid_credentials", "Invalid email or password",
                                HTTPStatus.UNAUTHORIZED)
        tenant_id = row["tenant_id"]
        account_id = self.accounts.authenticate(self.backend, tenant_id, email, password)
        session_id, session_token = self.sessions.create(
            self.backend, tenant_id, account_id, "member"
        )
        # Resolve the account's actual role from membership.
        with self.backend.transaction() as tx:
            member_row = tx.execute(
                "SELECT role FROM cloud_identity_members WHERE tenant_id = ? AND account_id = ?",
                (tenant_id, account_id),
            ).fetchone()
        role = member_row["role"] if member_row else "member"
        # Re-issue with the correct role.
        session_id, session_token = self.sessions.create(
            self.backend, tenant_id, account_id, role
        )
        return _json_response(HTTPStatus.OK, {
            "account_id": account_id,
            "tenant_id": tenant_id,
            "session_token": session_token,
            "role": role,
        })

    def handle_signout(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        self._authenticate(handler)
        token = _bearer_token(handler)
        if token:
            from weft_cloud.identity.tokens import hash_token
            token_hash = hash_token(token)
            # One transaction: revoke_by_token_hash does the lookup and the
            # UPDATE under a single writer lock. Calling sessions.revoke from
            # inside a nested transaction would BEGIN IMMEDIATE on a SECOND
            # connection — SQLite has one writer, so that inner BEGIN blocks
            # until this one commits, and this one cannot commit until the
            # inner returns: a 15s self-deadlock surfacing as a 500.
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
        if not email:
            raise _ServiceError("invalid_argument", "email is required")
        invite_id, raw_token = self.invites.create(ctx, email, role)
        return _json_response(HTTPStatus.CREATED, {
            "invite_id": invite_id,
            "invite_token": raw_token,
            "email": email,
            "role": role,
        })

    def handle_accept_invite(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        body = _read_body(handler)
        token = body.get("invite_token")
        email = body.get("email")
        password = body.get("password")
        if not token or not email or not password:
            raise _ServiceError("invalid_argument",
                                "invite_token, email, and password are required")
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
        vulnerability on this service. ``allow_self_owner`` lets the room
        create/connect endpoints accept an ``owner_agent_id`` that names the
        caller THEMSELVES (redundant, but harmless); the handler still verifies
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
        ACCOUNT, established by the session and never by the request body — so
        a caller can only ever resolve a room they are an active member of.
        Anything else yields the uniform ``room_not_found`` — identical to a
        fabricated room_id, so the endpoint is not a room-existence oracle.
        """
        try:
            with self.backend.transaction() as tx:
                return self.rooms._resolve_room_tenant(tx, room_id, agent_id)
        except RoomError:
            raise _ServiceError("room_not_found", "Room not found", HTTPStatus.NOT_FOUND)

    def handle_create_room(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body, allow_self_owner=True)
        owner_agent_id = body.get("owner_agent_id", ctx.account_id)
        if owner_agent_id != ctx.account_id:
            raise _ServiceError(
                "invalid_argument",
                "Identity is derived from your authenticated session; "
                "owner_agent_id must be your own account",
            )
        cap = body.get("cap", 10)
        name = body.get("name")
        ttl_seconds = body.get("ttl_seconds", 86400)
        # The actor token is derived from the session — recorded as an
        # informational hash only; room authorization is bound to the account.
        actor_token = _bearer_token(handler)
        result = self.rooms.create_room(
            ctx.tenant_id, owner_agent_id, actor_token, cap=cap,
            name=name, ttl_seconds=ttl_seconds, origin=self.origin,
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
        body = _read_body(handler)
        self._reject_identity_args(body, allow_self_owner=True)
        owner_agent_id = body.get("owner_agent_id", ctx.account_id)
        if owner_agent_id != ctx.account_id:
            raise _ServiceError(
                "invalid_argument",
                "Identity is derived from your authenticated session; "
                "owner_agent_id must be your own account",
            )
        cap = body.get("cap", 10)
        name = body.get("name")
        ttl_seconds = body.get("ttl_seconds", 86400)
        actor_token = _bearer_token(handler)
        result = self.rooms.create_room(
            ctx.tenant_id, owner_agent_id, actor_token, cap=cap,
            name=name, ttl_seconds=ttl_seconds, origin=self.origin,
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
                    "auth_scheme": "bearer-session-token",
                    "request": {
                        "room_id": room_id,
                        "link_token": link_token,
                        "agent_id": "<the joining agent's own id>",
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
            "auth_scheme": "bearer-session-token",
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
            ctx.tenant_id, room_id, link_token, ctx.account_id, consent, actor_token, capabilities,
        )
        self.backend.append_audit(
            ctx.tenant_id, "room.join", ctx.account_id, room_id,
            json.dumps({"agent_id": ctx.account_id}),
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
        # resolves as their OWN authenticated account.
        tenant_id = self._room_tenant(room_id, ctx.account_id)
        result = self.rooms.room_info(tenant_id, room_id, ctx.account_id)
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
        tenant_id = self._room_tenant(room_id, ctx.account_id)
        result = self.rooms.poll(tenant_id, room_id, ctx.account_id, after_seq, limit,
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
            tenant_id = self._room_tenant(room_id, ctx.account_id)
            result = self.rooms.wait(
                tenant_id, room_id, ctx.account_id,
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
        tenant_id = self._room_tenant(room_id, ctx.account_id)
        result = self.rooms.ack(tenant_id, room_id, ctx.account_id, seq)
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
        tenant_id = self._room_tenant(room_id, ctx.account_id)
        result = self.rooms.room_send(
            tenant_id, room_id, ctx.account_id, target_spec, payload, exclude_sender,
            message_kind=message_kind, idempotency_key=idempotency_key,
        )
        return _json_response(HTTPStatus.OK, result)

    def handle_room_leave(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, ctx.account_id)
        result = self.rooms.leave_room(tenant_id, room_id, ctx.account_id)
        return _json_response(HTTPStatus.OK, result)

    def handle_room_close(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, ctx.account_id)
        result = self.rooms.close_room(tenant_id, room_id, ctx.account_id)
        return _json_response(HTTPStatus.OK, result)

    def handle_revoke_link(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        link_id = body.get("link_id")
        if not room_id or not link_id:
            raise _ServiceError("invalid_argument", "room_id and link_id are required")
        tenant_id = self._room_tenant(room_id, ctx.account_id)
        result = self.rooms.revoke_link(tenant_id, room_id, ctx.account_id, link_id)
        return _json_response(HTTPStatus.OK, result)

    def handle_list_rooms(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        # The listing is scoped to the CALLER's account, never to a
        # client-chosen ``agent_id``. Accepting ?agent_id=<victim> let anyone
        # enumerate every room of any agent in every tenant — the pivot that
        # turns one impersonated room into everything (and the source of the
        # tenant_id needed for the signup takeover). The account is the only
        # identity this listing trusts.
        rooms = self._list_rooms_any_tenant(ctx.account_id)
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
        tenant_id = self._room_tenant(room_id, ctx.account_id)
        events = self.rooms.event_log(tenant_id, room_id, ctx.account_id)
        return _json_response(HTTPStatus.OK, {"events": events})

    def handle_room_heartbeat(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        self._reject_identity_args(body)
        room_id = body.get("room_id")
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, ctx.account_id)
        result = self.rooms.heartbeat(tenant_id, room_id, ctx.account_id)
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
        tenant_id = self._room_tenant(room_id, ctx.account_id)
        result = self.rooms.groups(tenant_id, room_id, ctx.account_id, group_name, action, members)
        return _json_response(HTTPStatus.OK, result)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _CloudHTTPHandler(BaseHTTPRequestHandler):
    """Routes HTTP requests to the service.

    Same idiom as ``weft_mcp/server.py``'s ``_MCPRequestHandler``:
    ``BaseHTTPRequestHandler`` with a class-level ``service`` reference.
    """

    service: WeftCloudService

    server_version = "weft-cloud/0.1.0"

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

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _handle_mcp_post(self) -> None:
        """Serve the authenticated, tenant-confined MCP endpoint at POST /mcp.

        Mirrors the coordinator's Streamable HTTP framing (``Mcp-Method`` /
        ``Mcp-Name`` header checks, JSON-RPC notifications -> 202) but every
        request must authenticate against the CLOUD identity plane. Auth is
        checked before any method is dispatched, so an unauthenticated
        initialize / tools/list / tools/call is refused alike and reveals
        nothing about the tool set or the store.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_JSON_RPC_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            _json_rpc_error(None, -32600, "Invalid request size"))
            return
        try:
            request = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, _json_rpc_error(None, -32700, "Parse error"))
            return
        method = request.get("method") if isinstance(request, dict) else None
        header_method = self.headers.get("Mcp-Method")
        header_name = self.headers.get("Mcp-Name")
        if header_method and header_method != method:
            self._send_json(HTTPStatus.BAD_REQUEST, _json_rpc_error(
                request.get("id"), -32600, "Mcp-Method does not match the JSON-RPC method"))
            return
        if method == "tools/call" and header_name and header_name != ((request.get("params") or {}).get("name")):
            self._send_json(HTTPStatus.BAD_REQUEST, _json_rpc_error(
                request.get("id"), -32600, "Mcp-Name does not match params.name"))
            return
        dispatcher = HostedMCPDispatcher(self.service)
        try:
            response = dispatcher.handle_json_rpc(request, _bearer_token(self))
        except HostedMCPAuthError:
            self._send_json(HTTPStatus.UNAUTHORIZED, _json_rpc_error(
                request.get("id") if isinstance(request, dict) else None, -32001, "Unauthorized"))
            return
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
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "Not found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/mcp":
            self._handle_mcp_post()
            return
        routes = {
            "/v1/auth/signup": self.service.handle_signup,
            "/v1/auth/signin": self.service.handle_signin,
            "/v1/auth/signout": self.service.handle_signout,
            "/v1/rooms/create": self.service.handle_create_room,
            "/v1/rooms/connect": self.service.handle_connect_room,
            "/v1/rooms/join": self.service.handle_join_room,
            "/v1/rooms/leave": self.service.handle_room_leave,
            "/v1/rooms/close": self.service.handle_room_close,
            "/v1/rooms/send": self.service.handle_room_send,
            "/v1/rooms/poll": self.service.handle_room_poll,
            "/v1/rooms/wait": self.service.handle_room_wait,
            "/v1/rooms/ack": self.service.handle_room_ack,
            "/v1/rooms/heartbeat": self.service.handle_room_heartbeat,
            "/v1/rooms/revoke_link": self.service.handle_revoke_link,
            "/v1/rooms/event_log": self.service.handle_event_log,
            "/v1/rooms/groups": self.service.handle_groups,
            "/v1/org/invite": self.service.handle_invite,
            "/v1/org/accept_invite": self.service.handle_accept_invite,
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
