"""Weft SDK — stdlib-only Python client for the Weft MCP protocol.

The SDK talks to a Weft MCP coordinator over JSON-RPC using stdlib
http.client.  It never imports weft_mcp — it is a pure client of the
protocol documented in README.md and docs/PROTOCOL.md.
"""

from __future__ import annotations

import http.client
import json
import os
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
    "member_required": AuthError,
    "owner_required": AuthError,
    "invalid_cursor": ConflictError,
}


def _raise_structured(code: str, message: str, details: Any | None = None) -> None:
    cls = _ERROR_MAP.get(code, WeftError)
    raise cls(code, message, details)


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
    "claim_task",
    "update_task",
    "verify_task",
    "complete_task",
    "send_message",
    "heartbeat",
    "rotate_agent_credential",
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

    def call(self, method: str, params: dict[str, Any], idempotency_key: str | None = None) -> Any:
        """Make a tools/call JSON-RPC call and return the structured result."""
        if idempotency_key is None:
            idempotency_key = f"sdk-{uuid.uuid4().hex}"
        request_body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": method, "arguments": params},
        }
        payload = json.dumps(request_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        is_idempotent = method in _IDEMPOTENT_METHODS
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
                    # Do NOT embed the coordinator response body into the raised
                    # exception — it can carry tokens. Redact it to status only.
                    raise WeftError(
                        "http_error",
                        f"HTTP {status} from coordinator",
                        {"status": status, "body": "<redacted>"},
                    )

                envelope = json.loads(body.decode("utf-8"))
                if "error" in envelope and envelope["error"]:
                    err = envelope["error"]
                    _raise_structured(err.get("code", "remote_error"), err.get("message", "Remote JSON-RPC error"), err.get("data"))
                result = envelope.get("result", {})
                if isinstance(result, dict) and result.get("isError"):
                    # WeftError returned as tool error content
                    content = result.get("content", [])
                    text = content[0].get("text", "") if content else ""
                    try:
                        inner = json.loads(text)
                        err = inner.get("error", {})
                        _raise_structured(err.get("code", "tool_error"), err.get("message", "Tool call failed"), err.get("details"))
                    except (json.JSONDecodeError, IndexError):
                        raise WeftError("tool_error", text or "Tool call failed")
                return result.get("structuredContent") if isinstance(result, dict) and "structuredContent" in result else result
            except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
                last_exc = exc
                if is_idempotent and attempt < _MAX_RETRIES - 1:
                    time.sleep(_BASE_BACKOFF * (2 ** attempt) + secrets.randbelow(10) / 100.0)
                    continue
                raise TimeoutError("transport_error", f"Transport failure: {exc}") from exc
        raise TimeoutError("transport_error", f"Transport failure after retries: {last_exc}")


# ---------------------------------------------------------------------------
# WeftClient
# ---------------------------------------------------------------------------

class WeftClient:
    """Ergonomic stdlib-only Python SDK for the Weft A2A protocol.

    Token hygiene: the actor_token is accepted as a constructor argument or via
    the WEFT_ACTOR_TOKEN environment variable.  It is NEVER logged and
    NEVER appears in __repr__.
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
        self._transport = _JsonRpcTransport(coordinator_url, bearer_token=bearer_token, timeout=timeout)

    # -- representation -----------------------------------------------------

    def __repr__(self) -> str:
        return f"WeftClient(coordinator_url={self.coordinator_url!r}, agent_id={self.agent_id!r}, team_id={self.team_id!r})"

    # -- low-level RPC ------------------------------------------------------

    def _call(self, method: str, **kwargs: Any) -> Any:
        params: dict[str, Any] = {"team_id": self.team_id, "agent_id": self.agent_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        if self._actor_token:
            params["actor_token"] = self._actor_token
        return self._transport.call(method, params)

    # -- connection check ---------------------------------------------------

    def connect(self) -> dict[str, Any]:
        """Verify the coordinator is reachable; returns protocol info."""
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
        return self._call(
            "register_agent",
            name=name or self.agent_id,
            role=role,
            model=model,
            capabilities=capabilities,
            metadata=metadata,
        )

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
            envelope=result["envelope"],
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
