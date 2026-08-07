"""Finalisma Cloud HTTP service — the hosted SaaS surface.

This is the runnable HTTP service that binds the identity plane (accounts,
sessions, orgs) and the room plane (multi-use links, ordered event log,
addressing) into a product a person can sign up for and paste a link into N
agents.

Architecture (per docs/PRODUCT_ROADMAP.md §1):
  - ``src/finalisma_mcp/`` stays stdlib-only forever — UNTOUCHED by this module.
  - ``src/finalisma_cloud/`` MAY take dependencies, but this service stays
    stdlib-only to keep the dependency-free promise intact for v1.
  - HTTP transport uses the same idiom as ``finalisma_mcp/server.py``:
    ``http.server.ThreadingHTTPServer`` + ``BaseHTTPRequestHandler``.

The service is a thin JSON-RPC-over-HTTP layer. Every request authenticates
via a Bearer session token (``fss_``) except signup/signin. Room mutations
require the actor to be an active member; room reads are member-only.

One link, many agents: the room link (``/r/{room_id}#{token}``) is multi-use
up to the room cap. Any number of distinct agents can redeem it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http import HTTPStatus
from typing import Any, Mapping
from urllib.parse import urlsplit

from finalisma_cloud.identity import (
    AccountStore,
    AuthError,
    InviteStore,
    OrgStore,
    RoleError,
    SessionContext,
    SessionStore,
    ensure_identity_schema,
)
from finalisma_cloud.identity.schema import ensure_schema as _ensure_identity_schema
from finalisma_cloud.quotas import QuotaError
from finalisma_cloud.rate_limit import RateLimitedError
from finalisma_cloud.rooms import CloudRoomService, RoomError
from finalisma_cloud.storage import SqliteWalBackend, StorageBackend

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


class FinalismaCloudService:
    """The hosted SaaS: identity + rooms over one HTTP surface.

    Constructed with a storage backend. All state lives in that backend's
    SQLite-WAL file. Tenancy is structural — the backend scopes every query
    by ``WHERE tenant_id = ?``.
    """

    def __init__(self, backend: StorageBackend) -> None:
        self.backend = backend
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
        tenant_id = body.get("tenant_id") or f"tenant_{secrets.token_hex(8)}"
        if not email or not password:
            raise _ServiceError("invalid_argument", "email and password are required")
        if not isinstance(email, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise _ServiceError("invalid_argument", "email must be a valid email address")
        if not isinstance(password, str) or len(password) < 8:
            raise _ServiceError("invalid_argument", "password must be at least 8 characters")

        account_id, verification_token = self.accounts.signup(
            self.backend, tenant_id, email, password
        )
        # Auto-verify for the hosted preview (no email provider in v1).
        try:
            self.accounts.verify_email(self.backend, verification_token)
        except AuthError:
            pass
        # Issue a session so the user is signed in immediately.
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
        })

    def handle_signin(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        body = _read_body(handler)
        email = body.get("email")
        password = body.get("password")
        if not email or not password:
            raise _ServiceError("invalid_argument", "email and password are required")
        # Look up the tenant for this email.
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT tenant_id FROM cloud_identity_accounts WHERE email = ?", (email,)
            ).fetchone()
        if row is None:
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
        ctx = self._authenticate(handler)
        token = _bearer_token(handler)
        if token:
            from finalisma_cloud.identity.tokens import hash_token
            token_hash = hash_token(token)
            with self.backend.transaction() as tx:
                row = tx.execute(
                    "SELECT session_id FROM cloud_identity_sessions WHERE token_hash = ?",
                    (token_hash,),
                ).fetchone()
                if row:
                    self.sessions.revoke(self.backend, row["session_id"])
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

    def _resolve_room_tenant(self, room_id: str, agent_id: str) -> str:
        """Resolve the effective tenant_id for a room operation.

        A member may belong to a different tenant than the room's owner
        (cross-tenant join via link). We resolve via the membership row.
        """
        with self.backend.transaction() as tx:
            return self.rooms._resolve_room_tenant(tx, room_id, agent_id)

    def handle_create_room(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        owner_agent_id = body.get("owner_agent_id", ctx.account_id)
        cap = body.get("cap", 10)
        name = body.get("name")
        ttl_seconds = body.get("ttl_seconds", 86400)
        # The actor token is derived from the session — the room binds it.
        actor_token = _bearer_token(handler)
        result = self.rooms.create_room(
            ctx.tenant_id, owner_agent_id, actor_token, cap=cap,
            name=name, ttl_seconds=ttl_seconds,
        )
        # Audit.
        self.backend.append_audit(
            ctx.tenant_id, "room.create", ctx.account_id, result["room_id"],
            json.dumps({"cap": cap}),
        )
        return _json_response(HTTPStatus.CREATED, result)

    def handle_join_room(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        room_id = body.get("room_id")
        link_token = body.get("link_token")
        agent_id = body.get("agent_id", ctx.account_id)
        consent = body.get("consent", False)
        capabilities = body.get("capabilities", [])
        actor_token = _bearer_token(handler)
        if not room_id or not link_token:
            raise _ServiceError("invalid_argument", "room_id and link_token are required")
        result = self.rooms.join_room(
            ctx.tenant_id, room_id, link_token, agent_id, consent, actor_token, capabilities,
        )
        self.backend.append_audit(
            ctx.tenant_id, "room.join", ctx.account_id, room_id,
            json.dumps({"agent_id": agent_id}),
        )
        return _json_response(HTTPStatus.OK, result)

    def _room_tenant(self, room_id: str, agent_id: str) -> str:
        """Resolve the effective tenant for a room operation.

        Agents who joined via a cross-tenant link have their membership in
        the room's owning tenant, not their own. Resolve via membership.
        """
        try:
            return self._resolve_room_tenant(room_id, agent_id)
        except RoomError:
            raise _ServiceError("room_not_found", "Room not found", HTTPStatus.NOT_FOUND)

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
        agent_id = params.get("agent_id", ctx.account_id)
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id query parameter is required")
        tenant_id = self._room_tenant(room_id, agent_id)
        result = self.rooms.room_info(tenant_id, room_id, agent_id)
        return _json_response(HTTPStatus.OK, result)

    def handle_room_poll(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        room_id = body.get("room_id")
        agent_id = body.get("agent_id", ctx.account_id)
        after_seq = body.get("after_seq")
        limit = body.get("limit", 100)
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, agent_id)
        result = self.rooms.poll(tenant_id, room_id, agent_id, after_seq, limit)
        return _json_response(HTTPStatus.OK, result)

    def handle_room_ack(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        room_id = body.get("room_id")
        seq = body.get("seq")
        agent_id = body.get("agent_id", ctx.account_id)
        if not room_id or seq is None:
            raise _ServiceError("invalid_argument", "room_id and seq are required")
        tenant_id = self._room_tenant(room_id, agent_id)
        result = self.rooms.ack(tenant_id, room_id, agent_id, seq)
        return _json_response(HTTPStatus.OK, result)

    def handle_room_send(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        room_id = body.get("room_id")
        target_spec = body.get("target_spec", "*")
        payload = body.get("payload", {})
        exclude_sender = body.get("exclude_sender", True)
        sender_agent_id = body.get("sender_agent_id", ctx.account_id)
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, sender_agent_id)
        result = self.rooms.room_send(
            tenant_id, room_id, sender_agent_id, target_spec, payload, exclude_sender,
        )
        return _json_response(HTTPStatus.OK, result)

    def handle_room_leave(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        room_id = body.get("room_id")
        agent_id = body.get("agent_id", ctx.account_id)
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, agent_id)
        result = self.rooms.leave_room(tenant_id, room_id, agent_id)
        return _json_response(HTTPStatus.OK, result)

    def handle_room_close(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        room_id = body.get("room_id")
        caller_agent_id = body.get("caller_agent_id", ctx.account_id)
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, caller_agent_id)
        result = self.rooms.close_room(tenant_id, room_id, caller_agent_id)
        return _json_response(HTTPStatus.OK, result)

    def handle_revoke_link(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        room_id = body.get("room_id")
        link_id = body.get("link_id")
        owner_agent_id = body.get("owner_agent_id", ctx.account_id)
        if not room_id or not link_id:
            raise _ServiceError("invalid_argument", "room_id and link_id are required")
        tenant_id = self._room_tenant(room_id, owner_agent_id)
        result = self.rooms.revoke_link(tenant_id, room_id, owner_agent_id, link_id)
        return _json_response(HTTPStatus.OK, result)

    def handle_list_rooms(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        params = {}
        if "?" in handler.path:
            qs = handler.path.split("?", 1)[1]
            for pair in qs.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = v
        agent_id = params.get("agent_id", ctx.account_id)
        # List rooms: scan across all tenants where this agent is a member.
        # Since agent_id is globally unique per email, we look up membership
        # across all tenants.
        rooms = self._list_rooms_any_tenant(agent_id)
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
        room_id = body.get("room_id")
        agent_id = body.get("agent_id", ctx.account_id)
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, agent_id)
        events = self.rooms.event_log(tenant_id, room_id, agent_id)
        return _json_response(HTTPStatus.OK, {"events": events})

    def handle_room_heartbeat(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        room_id = body.get("room_id")
        agent_id = body.get("agent_id", ctx.account_id)
        if not room_id:
            raise _ServiceError("invalid_argument", "room_id is required")
        tenant_id = self._room_tenant(room_id, agent_id)
        result = self.rooms.heartbeat(tenant_id, room_id, agent_id)
        return _json_response(HTTPStatus.OK, result)

    def handle_groups(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        ctx = self._authenticate(handler)
        body = _read_body(handler)
        room_id = body.get("room_id")
        group_name = body.get("group_name")
        action = body.get("action", "list")
        members = body.get("members")
        agent_id = body.get("agent_id", ctx.account_id)
        if not room_id or not group_name:
            raise _ServiceError("invalid_argument", "room_id and group_name are required")
        tenant_id = self._room_tenant(room_id, agent_id)
        result = self.rooms.groups(tenant_id, room_id, agent_id, group_name, action, members)
        return _json_response(HTTPStatus.OK, result)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _CloudHTTPHandler(BaseHTTPRequestHandler):
    """Routes HTTP requests to the service.

    Same idiom as ``finalisma_mcp/server.py``'s ``_MCPRequestHandler``:
    ``BaseHTTPRequestHandler`` with a class-level ``service`` reference.
    """

    service: FinalismaCloudService

    server_version = "finalisma-cloud/0.1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Never log request bodies or tokens.
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str, handler_fn) -> None:
        try:
            status, body = handler_fn(self)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
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
            self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(
                json.dumps({"error": {"code": exc.code, "message": str(exc)}}))))
            self.send_header("Retry-After", retry_after)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(json.dumps(
                {"error": {"code": exc.code, "message": str(exc), "retry_after": float(exc.retry_after or 1.0)}}
            ).encode("utf-8"))
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
            self._send_json(HTTPStatus.OK, {"status": "ok", "service": "finalisma-cloud"})
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
        routes = {
            "/v1/auth/signup": self.service.handle_signup,
            "/v1/auth/signin": self.service.handle_signin,
            "/v1/auth/signout": self.service.handle_signout,
            "/v1/rooms/create": self.service.handle_create_room,
            "/v1/rooms/join": self.service.handle_join_room,
            "/v1/rooms/leave": self.service.handle_room_leave,
            "/v1/rooms/close": self.service.handle_room_close,
            "/v1/rooms/send": self.service.handle_room_send,
            "/v1/rooms/poll": self.service.handle_room_poll,
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


def create_service(db_path: str = ":memory:") -> FinalismaCloudService:
    """Create a cloud service backed by a SQLite-WAL database.

    ``db_path=":memory:"`` is used for tests. Pass a file path for persistence.
    """
    backend = SqliteWalBackend(db_path)
    backend.initialize()
    return FinalismaCloudService(backend)


def runtime_config(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve runtime settings for the cloud service launcher.

    Precedence is CLI argv → environment → defaults, so the original
    positional form (``service.py <port> <db-path>``) keeps working while
    containers configure entirely through the environment:

    - ``FINALISMA_HOST``  (default ``127.0.0.1``)
    - ``FINALISMA_PORT``  (default ``18788``)
    - ``FINALISMA_DB_PATH`` (default ``./data/finalisma-cloud.db``)

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

    host = environ.get("FINALISMA_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = _port("FINALISMA_PORT", environ.get("FINALISMA_PORT"), 18788)
    db_path = environ.get("FINALISMA_DB_PATH", "./data/finalisma-cloud.db").strip() \
        or "./data/finalisma-cloud.db"

    if len(argv) >= 1:
        port = _port("port", argv[0], port)
    if len(argv) >= 2:
        db_path = argv[1]

    return {"host": host, "port": port, "db_path": db_path}


def serve(host: str = "127.0.0.1", port: int = 18788, db_path: str = "./data/finalisma-cloud.db") -> None:
    """Run the cloud HTTP service (blocking)."""
    service = create_service(db_path)
    _CloudHTTPHandler.service = service
    server = ThreadingHTTPServer((host, port), _CloudHTTPHandler)
    print(f"finalisma-cloud listening on http://{host}:{port}", flush=True)
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
        print(f"finalisma-cloud: {exc}", file=sys.stderr)
        sys.exit(2)
    serve(cfg["host"], cfg["port"], cfg["db_path"])
