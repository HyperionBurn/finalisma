"""Weft SDK — stdlib-only Python client for the Weft MCP protocol.

The SDK talks to a Weft MCP coordinator over JSON-RPC using stdlib
http.client.  It never imports weft_mcp — it is a pure client of the
protocol documented in README.md and docs/PROTOCOL.md.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class WeftError(RuntimeError):
    """Structured error returned by the SDK."""

    def __init__(self, code: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.code: Final[str] = code
        self.message: Final[str] = message
        self.details: Any | None = details


class AuthError(WeftError):
    """Raised when the server rejects an actor credential."""
    pass


class EvidenceError(WeftError):
    """Raised when the quality gate rejects submitted evidence."""
    pass


class NotFoundError(WeftError):
    """Raised when a referenced entity does not exist."""
    pass


class ConflictError(WeftError):
    """Raised when a state conflict (duplicate, race, stale token) occurs."""
    pass


class TimeoutError(WeftError):
    """Raised on transport timeout."""
    pass


_ERROR_MAP = {
    "actor_auth_required": AuthError,
    "actor_auth_invalid": AuthError,
    "consent_required": AuthError,
    "session_unauthorized": AuthError,
    "session_forbidden": AuthError,
    "pairing_not_found": NotFoundError,
    "task_not_found": NotFoundError,
    "message_not_found": NotFoundError,
    "agent_not_registered": NotFoundError,
    "room_not_found": NotFoundError,
    "link_not_found": NotFoundError,
    "quality_gate_required": EvidenceError,
    "quality_gate_failed": EvidenceError,
    "pairing_expired": ConflictError,
    "pairing_unavailable": ConflictError,
    "pairing_race": ConflictError,
    "task_claim_conflict": ConflictError,
    "scope_lock_conflict": ConflictError,
    "stale_fencing_token": ConflictError,
    "lease_expired": ConflictError,
    "state_conflict": ConflictError,
    "room_full": ConflictError,
    "room_closed": ConflictError,
    "link_revoked": ConflictError,
    "link_expired": ConflictError,
    "invalid_link": ConflictError,
    "member_removed": AuthError,
    "member_required": AuthError,
    "owner_required": AuthError,
    "invalid_cursor": ConflictError,
}


def _raise_structured(code: str, message: str, details: Any | None = None) -> None:
    cls = _ERROR_MAP.get(code, WeftError)
    raise cls(code, message, details)


# Client-supplied identity arguments the HOSTED dispatcher rejects
# (mirror of weft_cloud/mcp.py _FORBIDDEN_IDENTITY_ARGS — the SDK is a pure
# protocol client and cannot import weft_cloud). When the client is in hosted
# mode (a bearer credential was supplied), these are stripped from every tool
# call: identity is derived from the authenticated session, never an argument.
_HOSTED_FORBIDDEN_IDENTITY_ARGS = frozenset({
    "team_id",
    "tenant_id",
    "agent_id",
    "actor_token",
    "owner_agent_id",
    "sender_agent_id",
    "caller_agent_id",
})

# A structured error code is safe to adopt from a non-200 body only if it is a
# short snake_case identifier. This keeps the redaction contract intact: an
# arbitrary body (which may carry tokens) can never be echoed into the
# exception, only a well-formed machine code is.
_SAFE_HTTP_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _safe_retry_after(value: Any) -> int | float | None:
    """Return bounded numeric retry metadata, never arbitrary remote data."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or value > 86400:
        return None
    return value


def _safe_retry_details(error: dict[str, Any]) -> dict[str, int | float] | None:
    """Keep only the bounded retry hint from a remote error envelope."""
    nested = error.get("details")
    value = nested.get("retry_after") if isinstance(nested, dict) else None
    if value is None:
        value = error.get("retry_after")
    retry_after = _safe_retry_after(value)
    return {"retry_after": retry_after} if retry_after is not None else None


# ---------------------------------------------------------------------------
# Typed results
# ---------------------------------------------------------------------------

@dataclass
class TaskResult:
    task_id: str
    team_id: str
    title: str
    description: str
    status: str
    priority: int
    claimed_by: str | None = None
    fencing_token: int | None = None
    scope: list[str] = field(default_factory=list)
    progress: int = 0
    created_by: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PairingResult:
    pairing_id: str
    team_id: str
    join_url: str
    join_token: str
    display_code: str
    expires_at: float
    capabilities_offered: list[str] = field(default_factory=list)


@dataclass
class JoinResult:
    session_id: str
    session_token: str
    team_id: str
    state: str
    members: list[str]
    actor_token: str | None = None


@dataclass
class SessionEvent:
    session_id: str
    seq: int
    event_id: str
    origin_agent: str
    type: str
    payload: Any
    timestamp: str
    trace_id: str | None = None


@dataclass
class CredentialRotation:
    team_id: str
    agent_id: str
    actor_token: str
    rotation_count: int
    bootstrapped: bool = False


# ---------------------------------------------------------------------------
# Room types (Wave SDK-ROOMS) — first-class room surface
# ---------------------------------------------------------------------------

@dataclass
class RoomResult:
    room_id: str
    link_id: str
    link_token: str
    expires_at: float
    cap: int
    state: str
    owner_agent_id: str
    shareable_link: str = ""


@dataclass
class RoomJoinResult:
    room_id: str
    agent_id: str
    status: str
    joined_at: str
    cursor: int


@dataclass
class RoomMember:
    agent_id: str
    status: str
    capabilities: list[str]
    last_seen: float
    joined_at: str


@dataclass
class RoomInfo:
    room_id: str
    state: str
    cap: int
    member_count: int
    owner_agent_id: str
    members: list[RoomMember]


@dataclass
class RoomEvent:
    event_id: str
    seq: int
    origin_agent: str
    kind: str
    payload: Any
    created_at: str
    message_kind: str | None = None


@dataclass
class RoomPoll:
    room_id: str
    state: str
    events: list[RoomEvent]
    next_seq: int
    cursor_head: int
    last_ack_seq: int
    has_more: bool
    behind_by: int | None = None
    timed_out: bool = False


@dataclass
class RoomSendResult:
    room_id: str
    seq: int
    envelope: dict[str, Any]
    receipts: list[dict[str, Any]]


def _task_from_dict(d: dict[str, Any]) -> TaskResult:
    return TaskResult(
        task_id=d.get("task_id", ""),
        team_id=d.get("team_id", ""),
        title=d.get("title", ""),
        description=d.get("description", ""),
        status=d.get("status", ""),
        priority=d.get("priority", 2),
        claimed_by=d.get("claimed_by"),
        fencing_token=d.get("fencing_token"),
        scope=d.get("scope", []),
        progress=d.get("progress", 0),
        created_by=d.get("created_by", ""),
        raw=d,
    )


# ---------------------------------------------------------------------------
# HTTP / JSON-RPC transport with stdlib-only retry
# ---------------------------------------------------------------------------

_MAX_RETRIES = 4
_BASE_BACKOFF = 0.05  # seconds
_TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}
_IDEMPOTENT_METHODS = {
    "register_agent",
    "create_task",
    "send_message",
    "room_send",
    "heartbeat",
    "session_send",
    "session_ack",
    "read_inbox",
    "ack_message",
    "pairing_preview",
    "team_status",
    "protocol",
    "model_catalog",
    "session_poll",
    "session_status",
    "route_task",
}

# Only these methods have a server-side idempotency record keyed by the
# request argument.  Keep this narrower than _IDEMPOTENT_METHODS: several
# mutations are safe to retry because they are monotonic or transactional,
# but reject an unknown ``idempotency_key`` argument at the protocol boundary.
_IDEMPOTENCY_KEY_METHODS = frozenset({
    "create_task",
    "send_message",
    "room_send",
    "session_send",
})


class _JsonRpcTransport:
    """stdlib-only JSON-RPC POST /mcp transport with exponential backoff."""

    def __init__(self, base_url: str, bearer_token: str | None = None, timeout: float = 30.0):
        parsed = urlsplit(base_url)
        self._scheme = parsed.scheme
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port
        self._path = parsed.path or "/mcp"
        self._bearer = bearer_token
        self._timeout = timeout
        self._lock = threading.Lock()
        self._id_counter = 0

    def _endpoint(self) -> str:
        """``scheme://host:port`` for diagnostics. Never carries a credential —
        the bearer token travels in a header, and base_url userinfo is dropped
        by ``urlsplit(...).hostname``.
        """
        scheme = self._scheme or "http"
        if self._port:
            return f"{scheme}://{self._host}:{self._port}"
        return f"{scheme}://{self._host}"

    def _connection(self) -> http.client.HTTPConnection | http.client.HTTPSConnection:
        if self._scheme == "https":
            cls = http.client.HTTPSConnection
        else:
            cls = http.client.HTTPConnection
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._port:
            kwargs["port"] = self._port
        return cls(self._host, **kwargs)

    def _next_id(self) -> int:
        with self._lock:
            self._id_counter += 1
            return self._id_counter

    def close(self) -> None:
        pass  # connections are per-call; nothing pooled

    def initialize(self, protocol_version: str = "2025-11-25") -> dict[str, Any]:
        """Run the MCP initialize handshake and return its result object."""
        request_body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "weft-sdk", "version": "0.1.0"},
            },
        }
        payload = json.dumps(request_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                conn = self._connection()
                headers = {"Content-Type": "application/json", "Content-Length": str(len(payload))}
                if self._bearer:
                    headers["Authorization"] = f"Bearer {self._bearer}"
                conn.request("POST", self._path, body=payload, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                body = resp.read()
                retry_after = resp.getheader("Retry-After")
                conn.close()

                if status in _TRANSIENT_STATUSES and attempt < _MAX_RETRIES - 1:
                    time.sleep(_BASE_BACKOFF * (2 ** attempt) + secrets.randbelow(10) / 100.0)
                    continue
                if status != 200:
                    raise self._http_error(status, body, retry_after)

                envelope = json.loads(body.decode("utf-8"))
                if not isinstance(envelope, dict):
                    raise WeftError("remote_error", "Remote JSON-RPC response is invalid")
                if envelope.get("error"):
                    error = envelope["error"]
                    if not isinstance(error, dict):
                        raise WeftError("remote_error", "Remote JSON-RPC error")
                    code = error.get("code")
                    if not isinstance(code, str) or not _SAFE_HTTP_ERROR_CODE.fullmatch(code):
                        code = "remote_error"
                    _raise_structured(code, "Remote JSON-RPC error", _safe_retry_details(error))
                result = envelope.get("result")
                if not isinstance(result, dict):
                    raise WeftError("remote_error", "Remote JSON-RPC result is invalid")
                return result
            except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_BASE_BACKOFF * (2 ** attempt) + secrets.randbelow(10) / 100.0)
                    continue
                raise TimeoutError(
                    "transport_error",
                    f"Could not reach the Weft coordinator at {self._endpoint()} "
                    f"({exc}). Check the service is running and the base URL is correct.",
                ) from exc
        raise TimeoutError(
            "transport_error",
            f"Could not reach the Weft coordinator at {self._endpoint()} after "
            f"{_MAX_RETRIES} attempts ({last_exc}). Check the service is running "
            f"and the base URL is correct.",
        )

    @staticmethod
    def _http_error(status: int, body: bytes, retry_after_header: str | None) -> WeftError:
        """Raise a structured WeftError from a non-200 response.

        The hosted service refuses with HTTP 429 + a structured body
        {"error": {"code": "rate_limited", ..., "retry_after": N}} plus a
        Retry-After header. The structured ``code`` and ``retry_after`` are
        extracted so a caller can back off — while the server's own message
        text is NEVER adopted: the body can carry tokens, so only a validated
        machine code (short snake_case identifier) and a numeric retry_after
        ever reach the exception.
        """
        code = "http_error"
        retry_after: int | float | None = None
        parsed: Any = None
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                candidate = err.get("code")
                if isinstance(candidate, str) and _SAFE_HTTP_ERROR_CODE.fullmatch(candidate):
                    code = candidate
                ra = err.get("retry_after")
                if isinstance(ra, bool):
                    pass
                elif (safe_retry_after := _safe_retry_after(ra)) is not None:
                    retry_after = safe_retry_after
                elif isinstance(ra, str) and ra.strip().isdigit():
                    retry_after = float(ra.strip())
        if retry_after is None and isinstance(retry_after_header, str) and retry_after_header.strip().isdigit():
            retry_after = float(retry_after_header.strip())
        details: dict[str, Any] = {"status": status}
        if retry_after is not None:
            details["retry_after"] = retry_after
        return WeftError(code, f"HTTP {status} from coordinator", details)

    def call(self, method: str, params: dict[str, Any], idempotency_key: str | None = None) -> Any:
        """Make a tools/call JSON-RPC call and return the structured result."""
        if idempotency_key is None:
            idempotency_key = f"sdk-{uuid.uuid4().hex}"
        supports_key = method in _IDEMPOTENCY_KEY_METHODS and (
            method != "room_send" or self._bearer is not None
        )
        if supports_key:
            # The same serialized arguments are sent on every attempt.  For
            # keyed mutations, put the transport key in the JSON-RPC
            # arguments because that is what the server uses for deduplication.
            params = dict(params)
            if params.get("idempotency_key") is None:
                params["idempotency_key"] = idempotency_key
            else:
                # A public wrapper may have supplied its own key; use that
                # value for both the first request and all retries.
                idempotency_key = params["idempotency_key"]
        request_body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": method, "arguments": params},
        }
        payload = json.dumps(request_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        is_idempotent = method in _IDEMPOTENT_METHODS and (
            method != "room_send" or self._bearer is not None
        )
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES if is_idempotent else 1):
            try:
                conn = self._connection()
                headers = {"Content-Type": "application/json", "Content-Length": str(len(payload))}
                if self._bearer:
                    headers["Authorization"] = f"Bearer {self._bearer}"
                if idempotency_key:
                    headers["Mcp-Name"] = method
                conn.request("POST", self._path, body=payload, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                body = resp.read()
                conn.close()

                if status in _TRANSIENT_STATUSES and is_idempotent and attempt < _MAX_RETRIES - 1:
                    time.sleep(_BASE_BACKOFF * (2 ** attempt) + secrets.randbelow(10) / 100.0)
                    continue

                if status != 200:
                    raise self._http_error(status, body, resp.getheader("Retry-After"))

                envelope = json.loads(body.decode("utf-8"))
                if "error" in envelope and envelope["error"]:
                    err = envelope["error"]
                    if not isinstance(err, dict):
                        raise WeftError("remote_error", "Remote JSON-RPC error")
                    code = err.get("code")
                    if not isinstance(code, str) or not _SAFE_HTTP_ERROR_CODE.fullmatch(code):
                        code = "remote_error"
                    _raise_structured(code, "Remote JSON-RPC error", _safe_retry_details(err))
                result = envelope.get("result", {})
                if isinstance(result, dict) and result.get("isError"):
                    # WeftError returned as tool error content.  The content
                    # is remote input: only adopt it when it has the complete
                    # structured shape emitted by the coordinator.  In every
                    # malformed case, use fixed values so an arbitrary tool
                    # error body (which may carry a token) cannot reach the
                    # caller's exception.
                    content = result.get("content", [])
                    if (
                        not isinstance(content, list)
                        or not content
                        or not isinstance(content[0], dict)
                        or content[0].get("type") != "text"
                    ):
                        raise WeftError("tool_error", "Tool call failed")
                    text = content[0].get("text")
                    if not isinstance(text, str):
                        raise WeftError("tool_error", "Tool call failed")
                    try:
                        inner = json.loads(text)
                    except json.JSONDecodeError:
                        raise WeftError("tool_error", "Tool call failed")
                    if not isinstance(inner, dict) or not isinstance(inner.get("error"), dict):
                        raise WeftError("tool_error", "Tool call failed")
                    err = inner["error"]
                    code = err.get("code")
                    if (
                        not isinstance(code, str)
                        or not _SAFE_HTTP_ERROR_CODE.fullmatch(code)
                    ):
                        raise WeftError("tool_error", "Tool call failed")
                    _raise_structured(code, "Tool call failed", _safe_retry_details(err))
                return result.get("structuredContent") if isinstance(result, dict) and "structuredContent" in result else result
            except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
                last_exc = exc
                if is_idempotent and attempt < _MAX_RETRIES - 1:
                    time.sleep(_BASE_BACKOFF * (2 ** attempt) + secrets.randbelow(10) / 100.0)
                    continue
                raise TimeoutError(
                    "transport_error",
                    f"Could not reach the Weft coordinator at {self._endpoint()} "
                    f"({exc}). Check the service is running and the base URL is correct.",
                ) from exc
        raise TimeoutError(
            "transport_error",
            f"Could not reach the Weft coordinator at {self._endpoint()} after "
            f"{_MAX_RETRIES} attempts ({last_exc}). Check the service is running "
            f"and the base URL is correct.",
        )


# ---------------------------------------------------------------------------
# WeftClient
# ---------------------------------------------------------------------------

class WeftClient:
    """Ergonomic stdlib-only Python SDK for the Weft A2A protocol.

    Token hygiene: the actor_token is accepted as a constructor argument or via
    the WEFT_ACTOR_TOKEN environment variable.  It is NEVER logged and
    NEVER appears in __repr__.

    Hosted mode: when a ``bearer_token`` (an ``fss_`` session or ``agk_``
    agent key for the hosted /mcp surface) is supplied, the client is in
    hosted mode and never injects identity arguments (team_id / agent_id /
    actor_token / owner_agent_id / sender_agent_id / caller_agent_id) into
    tool calls — the hosted dispatcher derives identity from the
    authenticated credential and rejects client-supplied identity arguments.
    """

    def __init__(
        self,
        coordinator_url: str,
        agent_id: str,
        team_id: str,
        actor_token: str | None = None,
        bearer_token: str | None = None,
        timeout: float = 30.0,
    ):
        self.coordinator_url = coordinator_url
        self.agent_id = agent_id
        self.team_id = team_id
        self._actor_token = actor_token or os.environ.get("WEFT_ACTOR_TOKEN")
        self._bearer_token = bearer_token
        self._transport = _JsonRpcTransport(coordinator_url, bearer_token=bearer_token, timeout=timeout)

    # -- representation -----------------------------------------------------

    def __repr__(self) -> str:
        return f"WeftClient(coordinator_url={self.coordinator_url!r}, agent_id={self.agent_id!r}, team_id={self.team_id!r})"

    # -- low-level RPC ------------------------------------------------------

    def _call(self, method: str, **kwargs: Any) -> Any:
        if self._bearer_token:
            # Hosted mode: identity is derived from the authenticated bearer
            # credential. Never inject team_id/agent_id/actor_token — the
            # hosted dispatcher refuses them — and strip the identity
            # arguments room methods inject for the self-hosted surface.
            params = {k: v for k, v in kwargs.items() if v is not None}
            for forbidden in _HOSTED_FORBIDDEN_IDENTITY_ARGS:
                params.pop(forbidden, None)
        else:
            params = {"team_id": self.team_id, "agent_id": self.agent_id}
            params.update({k: v for k, v in kwargs.items() if v is not None})
            if self._actor_token:
                params["actor_token"] = self._actor_token
        return self._transport.call(method, params)

    # -- connection check ---------------------------------------------------

    def connect(self) -> dict[str, Any]:
        """Verify the coordinator is reachable; returns protocol info."""
        if self._bearer_token:
            return self._transport.initialize()
        return self._transport.call("protocol", {})

    # -- identity ------------------------------------------------------------

    def register(
        self,
        name: str | None = None,
        role: str = "generalist",
        model: str | None = None,
        capabilities: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register (or renew) this agent identity.  Returns the server response
        which includes a one-time actor_token on first registration."""
        result = self._call(
            "register_agent",
            name=name or self.agent_id,
            role=role,
            model=model,
            capabilities=capabilities,
            metadata=metadata,
        )
        # Registration returns the one-time actor credential. Keep it in this
        # client so the documented register() -> protected room operation flow
        # works without reaching into private state. A renewal may omit the
        # token, so never erase an already-valid credential in that case.
        if isinstance(result, dict):
            token = result.get("actor_token")
            if isinstance(token, str) and token:
                self._actor_token = token
        return result

    def heartbeat(self, task_ids: list[str] | None = None, fencing_tokens: dict[str, int] | None = None) -> dict[str, Any]:
        return self._call("heartbeat", task_ids=task_ids, fencing_tokens=fencing_tokens)

    def rotate_credential(self, current_token: str | None = None) -> CredentialRotation:
        token = current_token or self._actor_token or ""
        result = self._transport.call(
            "rotate_agent_credential",
            {"team_id": self.team_id, "agent_id": self.agent_id, "current_token": token},
        )
        new_token = result.get("actor_token", "")
        if new_token:
            self._actor_token = new_token
        return CredentialRotation(
            team_id=result.get("team_id", self.team_id),
            agent_id=result.get("agent_id", self.agent_id),
            actor_token=new_token,
            rotation_count=result.get("rotation_count", 0),
            bootstrapped=result.get("bootstrapped", False),
        )

    # -- pairing -------------------------------------------------------------

    def create_pairing_link(self, capabilities: list[str] | None = None, ttl_seconds: int = 900) -> PairingResult:
        result = self._call(
            "create_pairing",
            initiator_id=self.agent_id,
            capabilities_offered=capabilities,
            ttl_seconds=ttl_seconds,
        )
        return PairingResult(
            pairing_id=result["pairing_id"],
            team_id=result["team_id"],
            join_url=result["join_url"],
            join_token=result["join_token"],
            display_code=result.get("display_code", ""),
            expires_at=result.get("expires_at", 0.0),
            capabilities_offered=result.get("capabilities_offered", []),
        )

    def join_pairing(self, link: str, consent: bool = True, agent_id: str | None = None) -> JoinResult:
        """Join a pairing given a full join_url (token in fragment).

        The SDK extracts the token from the URL fragment per protocol and sends
        it in the JSON body. The caller's actor_token is injected so the join
        succeeds even when the coordinator requires actor credentials.
        """
        from urllib.parse import urlsplit, parse_qs
        parsed = urlsplit(link)
        fragment = parsed.fragment
        token = ""
        if fragment:
            qs = parse_qs(fragment)
            token = qs.get("token", [""])[0]
        if not token:
            raise WeftError("invalid_pairing_url", "Pairing URL must contain a #token= fragment")
        target_agent = agent_id or self.agent_id
        params: dict[str, Any] = {
            "token": token,
            "agent_id": target_agent,
            "name": target_agent,
            "role": "generalist",
            "consent": consent,
        }
        if self._actor_token:
            params["actor_token"] = self._actor_token
        result = self._transport.call(
            "join_pairing",
            params,
        )
        return JoinResult(
            session_id=result["session_id"],
            session_token=result["session_token"],
            team_id=result["team_id"],
            state=result.get("state", "active"),
            members=result.get("members", []),
            actor_token=result.get("actor_token"),
        )

    # -- tasks ---------------------------------------------------------------

    def create_task(
        self,
        scope: str | list[str] | None,
        description: str,
        priority: int = 2,
        capabilities: list[str] | None = None,
        title: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Create a task.  Returns the task_id."""
        if title is None:
            # Derive a short title from the description
            title = description[:60] if description else "untitled"
        result = self._call(
            "create_task",
            created_by=self.agent_id,
            title=title,
            description=description,
            scope=scope,
            priority=priority,
            idempotency_key=idempotency_key,
        )
        if result.get("created") and result.get("task"):
            return result["task"]["task_id"]
        if result.get("duplicate"):
            dup = result["duplicate"]
            raise ConflictError("duplicate_task", f"Similar open task already exists: {dup.get('task_id')}", dup)
        if result.get("idempotent"):
            return result["task"]["task_id"]
        # The server may return created=False without a duplicate when blocked
        if not result.get("created") and not result.get("idempotent"):
            raise WeftError("task_creation_failed", "Task was not created", result)
        raise WeftError("task_creation_failed", "Task was not created", result)

    def claim(self, task_id: str, lease_seconds: int | None = None) -> TaskResult:
        result = self._call("claim_task", task_id=task_id, lease_seconds=lease_seconds)
        return _task_from_dict(result)

    def update_progress(self, task_id: str, pct: int, note: str | None = None, fencing_token: int | None = None) -> TaskResult:
        result = self._call("update_task", task_id=task_id, progress=pct, note=note, fencing_token=fencing_token)
        return _task_from_dict(result)

    def submit_evidence(self, task_id: str, artifact_paths: list[str], checks: list[dict[str, Any]], fencing_token: int | None = None) -> dict[str, Any]:
        result = self._call(
            "verify_task",
            task_id=task_id,
            files=artifact_paths,
            checks=checks,
            fencing_token=fencing_token,
        )
        if not result.get("passed"):
            raise EvidenceError("quality_gate_failed", "Evidence did not pass the quality gate", result.get("details"))
        return result

    def complete(self, task_id: str, fencing_token: int | None = None, summary: str = "") -> TaskResult:
        result = self._call("complete_task", task_id=task_id, fencing_token=fencing_token, summary=summary)
        return _task_from_dict(result)

    # -- messaging -----------------------------------------------------------

    def ask(self, recipient: str, text: str, kind: str = "question") -> dict[str, Any]:
        return self._call(
            "send_message",
            sender_id=self.agent_id,
            recipient_id=recipient,
            kind=kind,
            payload={"text": text},
        )

    def send_envelope(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Send a fully-formed envelope dict.  Must include sender_id, kind, payload."""
        required = ("sender_id", "kind", "payload")
        for key in required:
            if key not in envelope:
                raise WeftError("invalid_envelope", f"Envelope must include '{key}'")
        return self._call("send_message", **envelope)

    # -- rooms (Wave SDK-ROOMS) ---------------------------------------------

    def create_room(self, cap: int, name: str | None = None, ttl_seconds: int = 86400,
                    **kwargs: Any) -> RoomResult:
        """Create a Room. Returns a RoomResult with the one multi-use link.

        The owner auto-joins as the first active member. `cap` is the maximum
        number of members (>= 2). Extra wire args pass through to the tool.

        `shareable_link` is an absolute, self-describing URL a second agent can
        open to discover the room, join endpoint, and protocol — hand it to the
        other agent instead of the raw token.
        """
        result = self._call(
            "room_create",
            owner_agent_id=kwargs.pop("owner_agent_id", self.agent_id),
            cap=cap,
            name=name,
            ttl_seconds=ttl_seconds,
            **kwargs,
        )
        return RoomResult(
            room_id=result["room_id"],
            link_id=result["link_id"],
            link_token=result["link_token"],
            expires_at=result["expires_at"],
            cap=result["cap"],
            state=result["state"],
            owner_agent_id=result["owner_agent_id"],
            shareable_link=result.get("shareable_link", ""),
        )

    def join_room(self, room_id: str, link_token: str, consent: bool = True,
                  capabilities: list[str] | None = None, agent_id: str | None = None,
                  **kwargs: Any) -> RoomJoinResult:
        """Join a Room with its multi-use link. consent must be a literal
        boolean True (the coordinator rejects strings). Returns a
        RoomJoinResult bound to this client's identity (or `agent_id` if given).
        """
        result = self._call(
            "room_join",
            room_id=room_id,
            link_token=link_token,
            agent_id=agent_id or self.agent_id,
            consent=consent,
            capabilities=capabilities,
            **kwargs,
        )
        return RoomJoinResult(
            room_id=result["room_id"],
            agent_id=result["agent_id"],
            status=result["status"],
            joined_at=result["joined_at"],
            cursor=result["cursor"],
        )

    def room_info(self, room_id: str, agent_id: str | None = None, **kwargs: Any) -> RoomInfo:
        """Member-only view of a Room: state, cap, member count, roster, owner."""
        result = self._call(
            "room_info",
            room_id=room_id,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )
        members = [
            RoomMember(
                agent_id=m["agent_id"],
                status=m["status"],
                capabilities=m.get("capabilities", []),
                last_seen=m["last_seen"],
                joined_at=m["joined_at"],
            )
            for m in result.get("members", [])
        ]
        return RoomInfo(
            room_id=result["room_id"],
            state=result["state"],
            cap=result["cap"],
            member_count=result["member_count"],
            owner_agent_id=result["owner_agent_id"],
            members=members,
        )

    def list_rooms(self, **kwargs: Any) -> list[dict[str, Any]]:
        """List rooms where this identity is an active member.

        Hosted clients can use this to discover old rooms before calling
        :meth:`close_room`. Closed rooms remain listed so the operation is
        observable and safe to repeat.
        """
        result = self._call("room_list", **kwargs)
        return list(result.get("rooms", []))

    def roster(self, room_id: str, agent_id: str | None = None, **kwargs: Any) -> list[RoomMember]:
        """Convenience: the member list from room_info."""
        return self.room_info(room_id, agent_id=agent_id, **kwargs).members

    def leave_room(self, room_id: str, agent_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """Leave a Room. The membership row is marked left; re-join reactivates."""
        return self._call(
            "room_leave",
            room_id=room_id,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )

    def close_room(self, room_id: str, owner_agent_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """Close a Room (owner only). Refuses joins and invalidates all links."""
        return self._call(
            "room_close",
            room_id=room_id,
            owner_agent_id=owner_agent_id or self.agent_id,
            **kwargs,
        )

    def revoke_link(self, room_id: str, link_id: str, owner_agent_id: str | None = None,
                    **kwargs: Any) -> dict[str, Any]:
        """Revoke a Room link (owner only) so it can admit no one."""
        return self._call(
            "room_revoke_link",
            room_id=room_id,
            link_id=link_id,
            owner_agent_id=owner_agent_id or self.agent_id,
            **kwargs,
        )

    def send(self, room_id: str, target=None, payload: Any = None, exclude_sender: bool = True,
             sender_agent_id: str | None = None, message_kind: str | None = None,
             **kwargs: Any) -> RoomSendResult:
        """Address one agent, a named group, or the whole room.

        `target` is the coordinator's target_spec: an agent_id (str), a group
        name (str), "*" for broadcast, or a list. The wire-level name
        `target_spec` is also accepted for symmetry. For broadcast the sender
        is excluded by default; pass exclude_sender=False to include it.
        `message_kind` is an optional sender-set label (lowercase [a-z0-9_-],
        max 32 chars) stored as a first-class, queryable column on the event —
        e.g. "result" for a finished unit of work, "status" for liveness.
        Returns a RoomSendResult with per-recipient delivery receipts.
        """
        target_spec = kwargs.pop("target_spec", target)
        if target_spec is None:
            raise WeftError("invalid_argument", "send requires a target (agent id, group, '*', or list)")
        result = self._call(
            "room_send",
            room_id=room_id,
            sender_agent_id=sender_agent_id or self.agent_id,
            target_spec=target_spec,
            payload=payload,
            message_kind=message_kind,
            exclude_sender=exclude_sender,
            **kwargs,
        )
        return RoomSendResult(
            room_id=result["room_id"],
            seq=result["seq"],
            envelope=result.get("envelope", {}),
            receipts=result["receipts"],
        )

    def group_members(self, room_id: str, group_name: str, agent_id: str | None = None,
                      **kwargs: Any) -> list[str]:
        """List the members of a named group within a Room."""
        kwargs.pop("action", None)  # this method always lists
        result = self._call(
            "room_groups",
            room_id=room_id,
            group_name=group_name,
            action="list",
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )
        return result["members"]

    def add_to_group(self, room_id: str, group_name: str, members: list[str],
                     agent_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """Add members to a named group for group-addressable sends."""
        return self._call(
            "room_groups",
            room_id=room_id,
            group_name=group_name,
            action="add",
            members=members,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )

    def remove_from_group(self, room_id: str, group_name: str, members: list[str],
                          agent_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """Remove members from a named group."""
        return self._call(
            "room_groups",
            room_id=room_id,
            group_name=group_name,
            action="remove",
            members=members,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )

    def room_poll(self, room_id: str, after_seq: int | None = None, limit: int = 100,
                  agent_id: str | None = None, message_kinds: list[str] | None = None,
                  **kwargs: Any) -> RoomPoll:
        """Replay ordered Room events from this member's cursor.

        With after_seq None the coordinator starts from the member's last ack.
        At-least-once; ack to advance this member's cursor.
        `message_kinds` optionally filters returned events to those whose
        message_kind matches an entry (e.g. ["result"] to consume only
        finished-work posts); when absent, everything is returned.
        """
        result = self._call(
            "room_poll",
            room_id=room_id,
            after_seq=after_seq,
            limit=limit,
            message_kinds=message_kinds,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )
        return self._poll_from_result(result)

    def room_wait(self, room_id: str, after_seq: int | None = None,
                  timeout_seconds: int = 20, limit: int = 100,
                  message_kinds: list[str] | None = None,
                  agent_id: str | None = None, **kwargs: Any) -> RoomPoll:
        """Block until another agent speaks in the Room, then return the new
        events after after_seq (same ordering, redaction, and cursor semantics
        as room_poll). Returns an EMPTY result at the timeout — that is
        normal, not an error — with timed_out=True. Blocks for up to
        timeout_seconds (the server clamps to a max of 30). Prefer this over
        room_poll when you expect a reply: it wakes the moment a message lands.
        """
        result = self._call(
            "room_wait",
            room_id=room_id,
            after_seq=after_seq,
            timeout_seconds=timeout_seconds,
            limit=limit,
            message_kinds=message_kinds,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )
        return self._poll_from_result(result)

    @staticmethod
    def _poll_from_result(result: dict[str, Any]) -> RoomPoll:
        events = [
            RoomEvent(
                event_id=e["event_id"],
                seq=e["seq"],
                origin_agent=e["origin_agent"],
                kind=e["kind"],
                payload=e.get("payload"),
                created_at=e.get("created_at", ""),
                message_kind=e.get("message_kind"),
            )
            for e in result.get("events", [])
        ]
        return RoomPoll(
            room_id=result["room_id"],
            state=result["state"],
            events=events,
            next_seq=result["next_seq"],
            cursor_head=result["cursor_head"],
            last_ack_seq=result["last_ack_seq"],
            has_more=result["has_more"],
            behind_by=result.get("behind_by"),
            timed_out=bool(result.get("timed_out", False)),
        )

    def room_ack(self, room_id: str, seq: int, agent_id: str | None = None, **kwargs: Any) -> int:
        """Advance this member's cursor to seq (monotonic MAX). Returns the
        new last_ack_seq."""
        result = self._call(
            "room_ack",
            room_id=room_id,
            seq=seq,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )
        return result["last_ack_seq"]

    def room_event_log(self, room_id: str, agent_id: str | None = None,
                       **kwargs: Any) -> list[RoomEvent]:
        """Return the full ordered event log for a Room (member-only audit
        surface). Payloads are redacted exactly as in room_poll: a non-addressee
        of a unicast sees the envelope, never the private body."""
        result = self._call(
            "room_event_log",
            room_id=room_id,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )
        return [
            RoomEvent(
                event_id=e["event_id"],
                seq=e["seq"],
                origin_agent=e["origin_agent"],
                kind=e["kind"],
                payload=e.get("payload"),
                created_at=e.get("created_at", ""),
                message_kind=e.get("message_kind"),
            )
            for e in result.get("events", [])
        ]

    def room_remove_member(self, room_id: str, member_id: str, agent_id: str | None = None,
                           **kwargs: Any) -> dict[str, Any]:
        """Owner-only: remove a member from a Room. The removed member is
        refused on its very next request and its seat is freed. The durable
        owner-removal marker refuses a later join until the owner restores it."""
        return self._call(
            "room_remove_member",
            room_id=room_id,
            member_id=member_id,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )

    def room_restore_member(self, room_id: str, member_id: str, agent_id: str | None = None,
                            **kwargs: Any) -> dict[str, Any]:
        """Owner-only: clear an owner-removal marker for a deliberate re-invite.

        The member remains inactive until it redeems the room's existing link.
        """
        return self._call(
            "room_restore_member",
            room_id=room_id,
            member_id=member_id,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )

    def room_heartbeat(self, room_id: str, agent_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """Refresh this member's presence in the Room."""
        return self._call(
            "room_heartbeat",
            room_id=room_id,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )

    def room_receipts(self, room_id: str, entry_ids: list[str], agent_id: str | None = None,
                      **kwargs: Any) -> list[dict[str, Any]]:
        """Query delivery-receipt status for outbox entry ids."""
        result = self._call(
            "room_receipts",
            room_id=room_id,
            entry_ids=entry_ids,
            agent_id=agent_id or self.agent_id,
            **kwargs,
        )
        return result["receipts"]

    # -- sessions ------------------------------------------------------------

    def session_send(self, session_token: str, kind: str, payload: Any, idempotency_key: str | None = None, trace_id: str | None = None, agent_id: str | None = None) -> SessionEvent:
        key = idempotency_key or f"sess-send-{uuid.uuid4().hex}"
        result = self._transport.call(
            "session_send",
            {
                "session_token": session_token,
                "agent_id": agent_id or self.agent_id,
                "kind": kind,
                "payload": payload,
                "idempotency_key": key,
                "trace_id": trace_id,
            },
        )
        if result.get("sent"):
            evt = result["event"]
        elif result.get("idempotent"):
            evt = result["event"]
        else:
            raise WeftError("session_send_failed", "Failed to send session event")
        return SessionEvent(
            session_id=evt["session_id"],
            seq=evt["seq"],
            event_id=evt["event_id"],
            origin_agent=evt["origin_agent"],
            type=evt["type"],
            payload=evt["payload"],
            timestamp=evt["timestamp"],
            trace_id=evt.get("trace_id"),
        )

    def session_wait(self, session_token: str, after_seq: int = 0, timeout_seconds: int = 20, limit: int = 100, agent_id: str | None = None) -> list[SessionEvent]:
        result = self._transport.call(
            "session_wait",
            {
                "session_token": session_token,
                "agent_id": agent_id or self.agent_id,
                "after_seq": after_seq,
                "timeout_seconds": timeout_seconds,
                "limit": limit,
            },
        )
        return [_session_event_from_dict(e) for e in result.get("events", [])]

    def session_poll(self, session_token: str, after_seq: int = 0, limit: int = 100, agent_id: str | None = None) -> list[SessionEvent]:
        result = self._transport.call(
            "session_poll",
            {
                "session_token": session_token,
                "agent_id": agent_id or self.agent_id,
                "after_seq": after_seq,
                "limit": limit,
            },
        )
        return [_session_event_from_dict(e) for e in result.get("events", [])]

    def session_ack(self, session_token: str, seq: int, agent_id: str | None = None) -> int:
        result = self._transport.call(
            "session_ack",
            {
                "session_token": session_token,
                "agent_id": agent_id or self.agent_id,
                "seq": seq,
            },
        )
        return result.get("last_ack_seq", seq)

    # -- cleanup -------------------------------------------------------------

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "WeftClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _session_event_from_dict(d: dict[str, Any]) -> SessionEvent:
    return SessionEvent(
        session_id=d.get("session_id", ""),
        seq=d.get("seq", 0),
        event_id=d.get("event_id", ""),
        origin_agent=d.get("origin_agent", ""),
        type=d.get("type", ""),
        payload=d.get("payload"),
        timestamp=d.get("timestamp", ""),
        trace_id=d.get("trace_id"),
    )
