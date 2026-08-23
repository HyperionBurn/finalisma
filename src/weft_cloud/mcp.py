"""Hosted MCP surface — an authenticated, tenant-confined MCP endpoint.

The self-hosted coordinator (``weft_mcp``) speaks MCP over ``POST /mcp`` with
an open ``register_agent``: anyone who can reach it can register into any
``team_id`` and read that team's rooms. Exposing that surface on the public
internet unchanged would let any caller open a room in any team.

This module mounts the MCP protocol on the HOSTED service instead. Every
request must present a valid CLOUD bearer credential (an ``fss_`` session or
an ``agk_`` agent key). The credential is resolved to a ``SessionContext``
(tenant + account) by the identity plane,
and every tool call is confined to that tenant by routing through
``CloudRoomService`` — the same service the ``/v1`` API and the web app
drive. A caller that presents no valid credential is refused with the same
generic error regardless of why the token is bad, so the endpoint leaks
nothing about which tenants, rooms, or agents exist.

Tools exposed (the room set the product promise depends on):

    room_create, room_list, room_join, room_send, room_receipts, room_poll,
    room_wait, room_info, room_ack, room_heartbeat, room_leave,
    room_remove_member, room_close, room_event_log

The full self-hosted 58-tool surface (``register_agent``, pairing, task,
roster, outbox, bridge, metrics, tenancy, …) is intentionally NOT exposed
here: those tools assume a self-hosted team/actor-token model and would each
need a per-tenant reimplementation to be safe to serve. A smaller correct
surface beats a large unsafe one.

Identity rules:

  - Agent identity is ALWAYS the authenticated identity — the ``account_id``
    for a session, a key-derived identity for an agent key — so one account
    running several keys gets several distinct room members. Client-supplied
    ``agent_id`` / ``actor_token`` / ``team_id`` / ``tenant_id`` /
    ``owner_agent_id`` / ``sender_agent_id`` / ``caller_agent_id`` arguments
    are rejected, so one account cannot impersonate another or mint arbitrary
    credentials.
  - The actor credential bound to room membership is the authenticated cloud
    bearer credential itself (the same convention ``/v1/rooms/create`` and
    ``/v1/rooms/join`` use), so
    a member identity created here is indistinguishable from one created over
    the REST surface.
  - Plan limits are enforced by ``CloudRoomService`` (the room member cap,
    atomically) exactly as on the ``/v1`` path; this surface is no weaker than
    ``/v1``.

Authoritative spec: docs/HOSTED_MCP_DESIGN.md.
"""

from __future__ import annotations

import json
import sys
import threading as _threading
from typing import Any, Callable

from weft_cloud.identity import AuthError, RoleError, SessionContext
from weft_cloud.quotas import DEFAULT_ROOM_CAP, QuotaError
from weft_cloud.rate_limit import RateLimitedError
from weft_cloud.rooms import CloudRoomService, RoomError
from weft_mcp.core import MCP_PROTOCOL_VERSION, SUPPORTED_MCP_VERSIONS, WeftError

SERVER_NAME = "weft-cloud"
SERVER_VERSION = "0.1.0"
MAX_JSON_RPC_BYTES = 512 * 1024

# Notification methods always yield no reply (202 with an empty body) even if
# a client sends them with an ``id``; the HTTP layer shares this set so its
# response-framing decision (plain 202 vs SSE vs JSON) matches
# ``handle_json_rpc``'s "returns None" contract exactly.
_MCP_NOTIFICATION_METHODS = frozenset({"notifications/initialized", "notifications/cancelled"})

# room_wait concurrency bound. A blocking long-poll holds one HTTP connection
# and one worker thread for up to timeout_seconds (clamped to 30). Many waiters
# is the NORMAL case for the product, so the cap is generous; only when it is
# exhausted is a caller refused FAST (never queued behind an unbounded thread
# pile-up) and can fall back to room_poll. The real per-call bound is the
# timeout clamp inside CloudRoomService.wait.
_WAIT_MAX_CONCURRENT = 128
_wait_slots = _threading.Semaphore(_WAIT_MAX_CONCURRENT)


class HostedMCPAuthError(Exception):
    """Transport-level auth failure: HTTP 401, generic, carries no detail.

    Raised for a missing token, a malformed token, an unknown token, a
    revoked session or agent key, and an expired session alike. The caller can
    never tell which, so the endpoint is not an oracle for credential validity.
    """


# ---------------------------------------------------------------------------
# Tool schema helpers (mirror of weft_mcp.server so client-facing shapes stay
# consistent between the self-hosted and hosted surfaces).
# ---------------------------------------------------------------------------


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_BOOLEAN = {"type": "boolean"}
_STRING_LIST = {"type": "array", "items": _STRING}
_JSON_VALUE = {}

#: ``target_spec`` deliberately accepts a string OR a list, so it cannot carry
#: a single JSON ``type``. It previously shipped as a bare ``{}``, which told a
#: caller NOTHING: the only broadcast token is ``*``, and an agent had no way
#: to discover that short of guessing (``all``, ``everyone``, ``room`` and
#: ``broadcast`` are all treated as member ids and refused). The description
#: and examples below are the schema's entire discoverability budget — keep
#: them accurate if the routing rules ever change.
_ROOM_CAP = {
    "type": "integer",
    "description": (
        "Maximum TOTAL members in the room, counting the owning account that "
        "auto-joins on creation. The join link therefore admits cap-1 further "
        "agents: cap=15 means the owner plus 14 joiners, NOT 15 joiners. Size "
        "it as (agents you want) + 1. Plan limits apply to this same total."
    ),
    "examples": [5, 15],
}

_TARGET_SPEC = {
    "description": (
        'Who receives this message. "*" addresses EVERY member of the room '
        '(broadcast) — it is the ONLY broadcast token; "all", "everyone", '
        '"room" and "broadcast" are treated as member ids and refused. A '
        'member id addresses that one member. A group name addresses that '
        'named group. A list may mix member ids and group names.'
    ),
    "examples": ["*", "acct_0123456789abcdef", ["acct_0123456789abcdef", "reviewers"]],
}


def _json_rpc_error(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _request_too_large_error(request_id: Any = None) -> dict[str, Any]:
    return _json_rpc_error(
        request_id,
        -32600,
        f"JSON-RPC request exceeds the {MAX_JSON_RPC_BYTES}-byte limit",
        {"code": "request_too_large", "max_bytes": MAX_JSON_RPC_BYTES},
    )


def _is_strict_json_value(value: Any) -> bool:
    """Return whether a decoded request contains only standard JSON values.

    Python's JSON decoder accepts NaN and infinities even though RFC 8259 does
    not. The hosted HTTP layer hands the decoded object to this module, so the
    dispatcher must enforce the same boundary before authentication or tool
    dispatch can persist one of those values.
    """
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# The hosted tool set. ``additionalProperties: False`` and the identity
# argument rejection in ``HostedMCPDispatcher.call_tool`` are both load-bearing:
# a client cannot slip ``team_id`` / ``agent_id`` / ``actor_token`` past the
# schema to reach a tool in another tenant.
# ---------------------------------------------------------------------------

HOSTED_TOOLS: list[dict[str, Any]] = [
    {
        "name": "room_create",
        "description": (
            "Create a Room in your tenant and get one multi-use join link. cap is "
            "the TOTAL member count INCLUDING the owning account, which auto-joins "
            "on creation — so the link admits cap-1 further agents (cap=15 means the "
            "owner plus 14 joiners). An agk_ key has its own agent identity separate "
            "from the account, so even the creator's key must call room_join with the "
            "returned room_id and link_token before room_poll or room_send will work; "
            "until it does, those calls answer room_not_found."
        ),
        "inputSchema": _object_schema({
            "cap": _ROOM_CAP,
            "name": _STRING,
            "ttl_seconds": _INTEGER,
        }, []),
    },
    {
        "name": "room_list",
        "description": (
            "List the Rooms where you are an active member, including closed rooms. "
            "Use this to find old Rooms before closing them. Results include each "
            "Room's state, owner, cap, name, and creation time."
        ),
        "inputSchema": _object_schema({}, []),
    },
    {
        "name": "room_join",
        "description": (
            "Join a Room with its multi-use link and explicit consent (literal boolean true). "
            "Your authenticated account becomes the member. The link admits new identities up to "
            "the cap; it cannot overwrite an existing member identity."
        ),
        "inputSchema": _object_schema({
            "room_id": _STRING,
            "link_token": _STRING,
            "consent": _BOOLEAN,
            "capabilities": _STRING_LIST,
        }, ["room_id", "link_token", "consent"]),
    },
    {
        "name": "room_send",
        "description": "Address one member, a named group, or the whole room with a payload, returning durable per-recipient receipts. Each receipt separates delivery status from recipient read_status; the sender may audit its own targeted message while other non-addressees receive a redacted envelope. Optional message_kind labels the event for filtered polling. Pass the same idempotency_key when retrying a send that may have succeeded but lost its response — the retry returns the original event's seq and receipts instead of duplicating.",
        "inputSchema": _object_schema({
            "room_id": _STRING,
            "target_spec": _TARGET_SPEC,
            "payload": _JSON_VALUE,
            "message_kind": _STRING,
            "exclude_sender": _BOOLEAN,
            "idempotency_key": _STRING,
        }, ["room_id", "target_spec", "payload"]),
    },
    {
        "name": "room_receipts",
        "description": (
            "Query current delivery/read state for entry ids from messages YOU "
            "sent in this Room. Unknown or non-owned entry ids return found:false "
            "without revealing another sender's outbox state."
        ),
        "inputSchema": _object_schema({
            "room_id": _STRING,
            "entry_ids": _STRING_LIST,
        }, ["room_id", "entry_ids"]),
    },
    {
        "name": "room_poll",
        "description": "Replay ordered Room events from your per-member cursor. At-least-once; ack to advance your own cursor. Reads are normally exclusive, while an unacknowledged next_seq resume marker is rechecked inclusively so truncated pages and idle-tail reconnects cannot skip an event. Optional message_kinds filters sender-labeled events while preserving the full-stream cursor.",
        "inputSchema": _object_schema({
            "room_id": _STRING,
            "after_seq": _INTEGER,
            "limit": _INTEGER,
            "message_kinds": _STRING_LIST,
        }, ["room_id"]),
    },
    {
        "name": "room_wait",
        "description": (
            "Block until another agent speaks in the Room, then return the new events "
            "after after_seq (same ordering, redaction, and cursor semantics as room_poll; "
            "an unacknowledged next_seq resume marker is rechecked inclusively). "
            "Returns an EMPTY result at the timeout; that is normal, not an error. "
            "Call this in a loop to stay in the conversation: wait, react, wait again. "
            "Blocks for up to timeout_seconds (default 20, max 30). Prefer this over "
            "room_poll when you expect a reply — it wakes the moment a message lands."
        ),
        "inputSchema": _object_schema({
            "room_id": _STRING,
            "after_seq": _INTEGER,
            "timeout_seconds": _INTEGER,
            "limit": _INTEGER,
            "message_kinds": _STRING_LIST,
        }, ["room_id"]),
    },
    {
        "name": "room_info",
        "description": "Member-only view of a Room: state, cap, member count, roster with presence, owner.",
        "inputSchema": _object_schema({"room_id": _STRING}, ["room_id"]),
    },
    {
        "name": "room_ack",
        "description": "Advance your cursor to seq (monotonic MAX). Events below the cursor are never re-delivered; durable receipt rows addressed to you are marked read through seq.",
        "inputSchema": _object_schema({
            "room_id": _STRING,
            "seq": _INTEGER,
        }, ["room_id", "seq"]),
    },
    {
        "name": "room_heartbeat",
        "description": "Refresh your presence (last_seen) in a Room.",
        "inputSchema": _object_schema({"room_id": _STRING}, ["room_id"]),
    },
    {
        "name": "room_leave",
        "description": (
            "Leave a Room you are a member of: your seat is freed immediately "
            "and you lose room access. History and attribution are preserved."
        ),
        "inputSchema": _object_schema({"room_id": _STRING}, ["room_id"]),
    },
    {
        "name": "room_remove_member",
        "description": (
            "Owner-only: remove a member from a Room. The removed member is refused "
            "on its very next request and its seat is freed. Removal is NOT a ban: "
            "a removed member who still holds a valid link can rejoin."
        ),
        "inputSchema": _object_schema({
            "room_id": _STRING,
            "member_id": _STRING,
        }, ["room_id", "member_id"]),
    },
    {
        "name": "room_close",
        "description": (
            "Owner-only: close a Room when you no longer need it. Closing revokes "
            "all share links, refuses new joins and sends, preserves the audit "
            "history, and releases the Room from your tenant's active-room quota. "
            "The operation is safe to repeat."
        ),
        "inputSchema": _object_schema({"room_id": _STRING}, ["room_id"]),
    },
    {
        "name": "room_event_log",
        "description": "Return the full ordered event log for a Room (member-only audit surface).",
        "inputSchema": _object_schema({"room_id": _STRING}, ["room_id"]),
    },
]

HOSTED_TOOL_NAMES = tuple(t["name"] for t in HOSTED_TOOLS)

# Client-supplied identity arguments that must never reach a tool. Identity is
# derived from the authenticated bearer credential; accepting these would let a caller
# impersonate another agent or mint arbitrary actor credentials.
_FORBIDDEN_IDENTITY_ARGS = frozenset({
    "team_id",
    "tenant_id",
    "agent_id",
    "actor_token",
    "owner_agent_id",
    "sender_agent_id",
    "caller_agent_id",
})


class HostedMCPDispatcher:
    """Tenant-confined MCP tool dispatcher over ``CloudRoomService``.

    One instance is cheap (it holds references only) and may be constructed
    per request. Every tool call is confined to ``ctx.tenant_id`` from the
    authenticated session; the tool implementation is identical to the
    ``/v1`` room handlers so the two surfaces share one service and one store.
    """

    def __init__(self, service: Any) -> None:
        self.service = service
        self.backend = service.backend
        self.rooms: CloudRoomService = service.rooms
        self.sessions = service.sessions
        self._keepalive: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self, token: str | None) -> SessionContext:
        """Resolve a bearer token to a SessionContext; raise auth error otherwise.

        Uses the service's single auth funnel: an ``fss_`` session or an
        ``agk_`` agent key both resolve to the SAME SessionContext and then
        flow through identical tenant/role checks. Missing, malformed, unknown,
        revoked, and expired tokens all raise the same ``HostedMCPAuthError``
        with no detail attached.
        """
        if not isinstance(token, str) or not token:
            raise HostedMCPAuthError()
        try:
            return self.service.resolve_identity(token)
        except AuthError:
            raise HostedMCPAuthError() from None

    # ------------------------------------------------------------------
    # MCP JSON-RPC framing (mirror of weft_mcp.server.handle_json_rpc)
    # ------------------------------------------------------------------

    def preauthenticate(
        self,
        request: Any,
        bearer_token: str | None = None,
        ctx: SessionContext | None = None,
    ) -> tuple[Any, str, dict, bool, SessionContext] | None:
        """Validate the JSON-RPC envelope and authenticate, WITHOUT dispatching.

        This is exactly the validation-and-auth prefix of ``handle_json_rpc``,
        factored out so the HTTP layer can pick the response framing (plain
        JSON vs an SSE stream) before any tool runs — while refusing every
        invalid shape and every bad credential identically to how
        ``handle_json_rpc`` would:

          - a malformed envelope returns ``None`` (the caller sends the same
            ``-32600 Invalid JSON-RPC request`` error ``handle_json_rpc``
            returns, with the same id),
          - an invalid credential raises ``HostedMCPAuthError`` (HTTP 401).

        Returns ``(request_id, method, params, is_notification, ctx)`` on
        success. ``ctx`` may be supplied when the caller already
        authenticated, to avoid a second credential lookup.
        """
        if not isinstance(request, dict) or not _is_strict_json_value(request) \
                or request.get("jsonrpc") != "2.0":
            return None
        request_id = request.get("id")
        method = request.get("method")
        # Params are optional, but when present this hosted surface accepts
        # only the object form used by MCP tool calls. Do not coerce false,
        # null, or an empty array into a valid empty object.
        params = request["params"] if "params" in request else {}
        if not isinstance(method, str) or not isinstance(params, dict):
            return None
        is_notification = "id" not in request
        if ctx is None:
            ctx = self.authenticate(bearer_token)
        return request_id, method, params, is_notification, ctx

    def handle_json_rpc(self, request: Any, bearer_token: str | None = None,
                        *, ctx: SessionContext | None = None,
                        keepalive: Callable[[], None] | None = None) -> dict[str, Any] | None:
        """Handle one MCP JSON-RPC request; return None for notifications.

        Authentication happens BEFORE any method is dispatched, so an
        unauthenticated initialize / tools/list / tools/call is refused alike
        and reveals nothing about the tool set or the store.

        ``ctx`` short-circuits authentication: the HTTP layer may
        pre-authenticate once via ``preauthenticate`` to choose the transport
        framing without a second credential lookup. ``keepalive`` is an
        optional callable the dispatcher invokes while a tool blocks (e.g.
        ``room_wait``), so an SSE transport can emit comment frames instead
        of sitting silent.
        """
        pre = self.preauthenticate(request, bearer_token, ctx=ctx)
        if pre is None:
            # Byte-identical to the pre-refactor refusals: a non-dict or
            # non-2.0 envelope carries id=None; a bad method/params carries
            # the request id.
            bad_id = request.get("id") if (isinstance(request, dict)
                                            and request.get("jsonrpc") == "2.0") else None
            return _json_rpc_error(bad_id, -32600, "Invalid JSON-RPC request")
        request_id, method, params, is_notification, ctx = pre
        self._keepalive = keepalive

        if method == "initialize":
            requested = params.get("protocolVersion")
            selected = requested if requested in SUPPORTED_MCP_VERSIONS else MCP_PROTOCOL_VERSION
            result = {
                "protocolVersion": selected,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "You are authenticated as a Weft cloud account; every tool call is confined "
                    "to that account's tenant. Your identity is never an argument. Room memberships "
                    "are created by redeeming room links with explicit consent."
                ),
            }
            return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": result}
        if method in _MCP_NOTIFICATION_METHODS:
            return None
        if method == "ping":
            return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": {"tools": HOSTED_TOOLS}}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return _json_rpc_error(request_id, -32602, "tools/call requires params.name")
            try:
                result = self.call_tool(ctx, name, params.get("arguments") or {}, bearer_token)
                tool_result = {
                    "content": [{"type": "text", "text": json.dumps(
                        result, ensure_ascii=False, indent=2, allow_nan=False)}],
                    "structuredContent": result,
                }
            except WeftError as exc:
                tool_result = {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(
                        {"error": exc.as_dict()}, ensure_ascii=False, allow_nan=False)}],
                }
            except RoomError as exc:
                tool_result = {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(
                        {"error": {"code": exc.code, "message": exc.message}},
                        ensure_ascii=False, allow_nan=False)}],
                }
            except RoleError as exc:
                tool_result = {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(
                        {"error": {"code": exc.code, "message": str(exc)},
                         }, ensure_ascii=False, allow_nan=False)}],
                }
            except RateLimitedError as exc:
                retry_after = float(exc.retry_after or 0.0)
                tool_result = {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps({
                        "error": {
                            "code": exc.code,
                            "message": f"Rate limit exceeded; retry after {retry_after:g} seconds",
                            "retry_after": retry_after,
                            "details": {"retry_after": retry_after},
                        }
                    }, ensure_ascii=False, allow_nan=False)}],
                }
            except QuotaError as exc:
                # Plan limit hit — the SAME code + message shape the /v1 REST
                # surface returns. Include the caller's OWN limit + plan so the
                # error is actionable (an agent can lower the cap and retry);
                # never another tenant's data or an internal id. Quota errors
                # are a normal, documented refusal — not an internal failure.
                error = {"code": exc.code, "message": str(exc)}
                if exc.limit_name is not None:
                    error["limit"] = {
                        "name": exc.limit_name,
                        "value": exc.limit_value,
                        "plan": exc.plan_id,
                    }
                tool_result = {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(
                        {"error": error}, ensure_ascii=False, allow_nan=False)}],
                }
            except AuthError as exc:
                # A credential can be revoked or expire after pre-auth but
                # before a room transaction validates it again. That is an
                # actionable auth race, not a server defect.
                tool_result = {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(
                        {"error": {"code": exc.code, "message": str(exc)}},
                        ensure_ascii=False, allow_nan=False)}],
                }
            except Exception as exc:  # pragma: no cover - defensive last-resort boundary
                print(f"weft-cloud MCP internal error: {type(exc).__name__}", file=sys.stderr)
                tool_result = {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(
                        {"error": {"code": "internal_error", "message": "The server could not complete the tool call"}},
                        allow_nan=False)}],
                }
            return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": tool_result}
        if method in {"resources/list", "prompts/list"}:
            empty = {"resources": []} if method == "resources/list" else {"prompts": []}
            return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": empty}
        return _json_rpc_error(request_id, -32601, f"Method not found: {method}")

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def call_tool(self, ctx: SessionContext, name: str, args: Any, bearer_token: str | None) -> Any:
        if not isinstance(args, dict):
            raise WeftError("invalid_argument", "Tool arguments must be a JSON object")
        self._reject_identity_args(args)
        tool = next((item for item in HOSTED_TOOLS if item["name"] == name), None)
        if tool is not None:
            allowed = set(tool["inputSchema"].get("properties", {}))
            unknown = sorted(set(args) - allowed)
            if unknown:
                raise WeftError(
                    "invalid_argument",
                    f"Unknown argument(s) for {name}: {', '.join(unknown)}",
                )
        handler = getattr(self, "_tool_" + name, None)
        if handler is None:
            raise WeftError("unknown_tool", f"Unknown tool '{name}'")
        return handler(ctx, args, bearer_token)

    def _reject_identity_args(self, args: dict[str, Any]) -> None:
        supplied = sorted(set(args) & _FORBIDDEN_IDENTITY_ARGS)
        if supplied:
            raise WeftError(
                "invalid_argument",
                f"Identity is derived from your authenticated session; argument(s) {', '.join(supplied)} are not accepted",
            )

    @staticmethod
    def _required(args: dict[str, Any], name: str) -> Any:
        if name not in args or args[name] is None:
            raise WeftError("invalid_argument", f"Missing required argument: {name}")
        return args[name]

    def _room_call(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except RoomError as exc:
            raise WeftError(exc.code, exc.message) from exc

    def _room_tenant(self, room_id: str, agent_id: str) -> str:
        """Resolve the effective tenant for a room operation via membership.

        Mirrors ``WeftCloudService._room_tenant``: a member who joined a room
        through a link in another tenant keeps their membership in the ROOM's
        tenant, not their session tenant. Resolution goes through the
        membership row (``agent_id``), so a caller can only ever resolve a
        room they are an active member of; everything else yields the uniform
        ``room_not_found`` (identical to a nonexistent room, so no oracle).
        """
        try:
            with self.backend.transaction() as tx:
                return self.rooms._resolve_room_tenant(tx, room_id, agent_id)
        except RoomError as exc:
            raise WeftError(exc.code, exc.message) from exc

    # ------------------------------------------------------------------
    # Room tools — 1:1 with the /v1 handlers. The tenant is resolved per
    # room via the caller's membership (so cross-tenant link members work,
    # exactly as on /v1); the agent identity is the session account or the
    # key-derived agent identity; the actor credential on create/join is the
    # authenticated bearer credential.
    # ------------------------------------------------------------------

    def _tool_room_create(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        cap = args.get("cap", DEFAULT_ROOM_CAP)
        if not isinstance(cap, int):
            raise WeftError("invalid_argument", "cap must be an integer")
        # Room creation is an organization-level mutation. Keep the fast
        # request guard aligned with REST, then pass the account identity into
        # the domain transaction so a concurrent demotion is rechecked before
        # any room/quota/member rows are written.
        ctx.require_role("admin")
        return self._room_call(lambda: self.rooms.create_room(
            tenant_id=ctx.tenant_id,
            owner_agent_id=ctx.account_id,
            actor_token=bearer_token,
            cap=cap,
            name=args.get("name"),
            ttl_seconds=args.get("ttl_seconds", 86400),
            actor_account_id=ctx.account_id,
        ))

    def _tool_room_list(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        rooms = self.rooms.list_rooms_for_member_any_tenant(ctx.agent_id)
        # Cross-tenant memberships are valid, but the connector does not need
        # to expose tenant identifiers to close a room. Room operations resolve
        # the effective tenant from the caller's membership instead.
        fields = ("room_id", "name", "state", "cap", "owner_agent_id", "created_at")
        return {"rooms": [{field: room[field] for field in fields} for room in rooms]}

    def _tool_room_join(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        return self._room_call(lambda: self.rooms.join_room(
            tenant_id=ctx.tenant_id,
            room_id=self._required(args, "room_id"),
            link_token=self._required(args, "link_token"),
            agent_id=ctx.agent_id,
            consent=args.get("consent"),
            actor_token=bearer_token,
            capabilities=args.get("capabilities") or [],
        ))

    def _tool_room_send(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        room_id = self._required(args, "room_id")
        return self._room_call(lambda: self.rooms.room_send(
            tenant_id=self._room_tenant(room_id, ctx.agent_id),
            room_id=room_id,
            sender_agent_id=ctx.agent_id,
            target_spec=self._required(args, "target_spec"),
            payload=self._required(args, "payload"),
            exclude_sender=bool(args.get("exclude_sender", True)),
            idempotency_key=args.get("idempotency_key"),
            message_kind=args.get("message_kind"),
        ))

    def _tool_room_receipts(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        room_id = self._required(args, "room_id")
        return self._room_call(lambda: self.rooms.receipts(
            tenant_id=self._room_tenant(room_id, ctx.agent_id),
            room_id=room_id,
            agent_id=ctx.agent_id,
            entry_ids=self._required(args, "entry_ids"),
        ))

    def _tool_room_poll(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        room_id = self._required(args, "room_id")
        return self._room_call(lambda: self.rooms.poll(
            tenant_id=self._room_tenant(room_id, ctx.agent_id),
            room_id=room_id,
            agent_id=ctx.agent_id,
            after_seq=args.get("after_seq"),
            limit=args.get("limit", 100),
            message_kinds=args.get("message_kinds"),
        ))

    def _tool_room_wait(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        room_id = self._required(args, "room_id")
        if not _wait_slots.acquire(blocking=False):
            raise WeftError(
                "wait_busy",
                "Too many room_wait calls are in flight; retry with room_poll or retry room_wait shortly",
            )
        try:
            return self._room_call(lambda: self.rooms.wait(
                tenant_id=self._room_tenant(room_id, ctx.agent_id),
                room_id=room_id,
                agent_id=ctx.agent_id,
                after_seq=args.get("after_seq"),
                timeout_seconds=args.get("timeout_seconds", 20),
                limit=args.get("limit", 100),
                message_kinds=args.get("message_kinds"),
                _pulse=self._keepalive,
            ))
        finally:
            _wait_slots.release()

    def _tool_room_info(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        room_id = self._required(args, "room_id")
        return self._room_call(lambda: self.rooms.room_info(
            tenant_id=self._room_tenant(room_id, ctx.agent_id),
            room_id=room_id,
            agent_id=ctx.agent_id,
            owner_agent_id=ctx.account_id,
        ))

    def _tool_room_ack(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        room_id = self._required(args, "room_id")
        return self._room_call(lambda: self.rooms.ack(
            tenant_id=self._room_tenant(room_id, ctx.agent_id),
            room_id=room_id,
            agent_id=ctx.agent_id,
            seq=self._required(args, "seq"),
        ))

    def _tool_room_heartbeat(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        room_id = self._required(args, "room_id")
        return self._room_call(lambda: self.rooms.heartbeat(
            tenant_id=self._room_tenant(room_id, ctx.agent_id),
            room_id=room_id,
            agent_id=ctx.agent_id,
        ))

    def _tool_room_leave(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        room_id = self._required(args, "room_id")
        return self._room_call(lambda: self.rooms.leave_room(
            tenant_id=self._room_tenant(room_id, ctx.agent_id),
            room_id=room_id,
            agent_id=ctx.agent_id,
        ))

    def _tool_room_remove_member(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        room_id = self._required(args, "room_id")
        member_id = self._required(args, "member_id")
        return self._room_call(lambda: self.rooms.remove_member(
            tenant_id=self._room_tenant(room_id, ctx.agent_id),
            room_id=room_id,
            owner_agent_id=ctx.account_id,
            target_agent_id=member_id,
            caller_agent_id=ctx.agent_id,
        ))

    def _tool_room_close(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        room_id = self._required(args, "room_id")
        return self._room_call(lambda: self.rooms.close_room(
            tenant_id=self._room_tenant(room_id, ctx.agent_id),
            room_id=room_id,
            caller_agent_id=ctx.agent_id,
            owner_agent_id=ctx.account_id,
        ))

    def _tool_room_event_log(self, ctx: SessionContext, args: dict[str, Any], bearer_token: str | None) -> dict[str, Any]:
        room_id = self._required(args, "room_id")
        return self._room_call(lambda: {"events": self.rooms.event_log(
            tenant_id=self._room_tenant(room_id, ctx.agent_id),
            room_id=room_id,
            agent_id=ctx.agent_id,
        )})
