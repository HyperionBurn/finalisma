"""Universal bridge adapters for non-MCP hosts.

This module implements adapters for hosts that CANNOT speak MCP but can still
participate in Finalisma coordination:

1. WebhookBridge — hosts that can receive HTTP POST callbacks (signed envelopes).
2. PollingBridge — hosts that can only pull (durable outbox + cursor checkpoint).
3. ClipboardBridge — CLI copy-paste tier (one-shot bootstrap JSON snippet).
4. HttpBridgeClient — tiny stdlib http.client wrapper for any process to use
   the "give a link" flow: preview, join, send, poll, ack.

All bridge calls are bound to (team_id, actor_id) + actor key proof.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import secrets
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Sequence

from .core import (
    FinalismaError,
    FinalismaStore,
    _validate_id,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class BridgeAuthError(FinalismaError):
    """Raised when bridge actor authentication fails."""

    def __init__(self, message: str = "Actor authentication failed") -> None:
        super().__init__("actor_auth_invalid", message)


class BridgeSignatureError(FinalismaError):
    """Raised when webhook signature verification fails."""

    def __init__(self, message: str = "Signature verification failed") -> None:
        super().__init__("invalid_signature", message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_epoch() -> float:
    return time.time()


def _token_hash(token: str) -> str:
    """SHA-256 hash of a secret — only hashes are stored."""
    if not isinstance(token, str) or len(token) < 8:
        raise FinalismaError("invalid_token", "Secret must be a string of at least 8 bytes")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hmac_sign(secret: bytes, timestamp: str, body: Any) -> str:
    """Produce HMAC-SHA256 signature for webhook delivery."""
    payload_str = f"{timestamp}.{json.dumps(body, sort_keys=True, separators=(',', ':'))}"
    return "sha256=" + hmac_mod.new(secret, payload_str.encode("utf-8"), hashlib.sha256).hexdigest()


def _authorize_actor(store: FinalismaStore, team_id: str, agent_id: str, actor_token: str | None) -> None:
    """Validate actor credential or raise BridgeAuthError."""
    if actor_token is None:
        raise BridgeAuthError("actor_token is required")
    try:
        supplied_hash = _token_hash(actor_token)
    except FinalismaError:
        raise BridgeAuthError("Actor token is invalid")
    with store._read() as conn:
        row = conn.execute(
            "SELECT token_hash FROM agent_credentials WHERE team_id = ? AND agent_id = ? AND revoked_at IS NULL",
            (team_id, agent_id),
        ).fetchone()
        if row is None:
            raise BridgeAuthError("No credential registered for this agent")
        if not secrets.compare_digest(row["token_hash"], supplied_hash):
            raise BridgeAuthError("Actor token mismatch")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

BRIDGE_SCHEMA_STATEMENTS = """
CREATE TABLE IF NOT EXISTS bridge_webhooks (
    webhook_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    url TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_delivered_at TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bridge_webhooks_agent ON bridge_webhooks(team_id, agent_id);

CREATE TABLE IF NOT EXISTS bridge_outbox (
    event_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    event_json TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    acked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bridge_outbox_poll ON bridge_outbox(team_id, agent_id, seq);
CREATE INDEX IF NOT EXISTS idx_bridge_outbox_ack ON bridge_outbox(event_id);

CREATE TABLE IF NOT EXISTS bridge_cursors (
    team_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    last_ack_seq INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (team_id, agent_id)
);

CREATE TABLE IF NOT EXISTS bridge_bootstrap_nonces (
    nonce TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
"""


def init_bridge(store: FinalismaStore) -> None:
    """Idempotently create bridge schema tables."""
    with store._transaction() as conn:
        conn.executescript(BRIDGE_SCHEMA_STATEMENTS)


# ---------------------------------------------------------------------------
# 1. WebhookBridge
# ---------------------------------------------------------------------------

class WebhookBridge:
    """Hosts that can receive HTTP POST callbacks get signed webhook delivery.

    Events are delivered with HMAC-SHA256 signature in the
    ``X-Finalisma-Signature`` header and a replay timestamp in
    ``X-Finalisma-Timestamp``.
    """

    def __init__(self, store: FinalismaStore) -> None:
        self.store = store
        init_bridge(store)

    def register_webhook(
        self,
        team_id: str,
        agent_id: str,
        url: str,
        secret_ref: str,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        """Register a webhook URL for an agent. Stores only SHA-256 of secret."""
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        _authorize_actor(self.store, team_id, agent_id, actor_token)

        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise FinalismaError("invalid_argument", "Webhook URL must be an http(s) URL")
        if len(secret_ref) < 8:
            raise FinalismaError("invalid_argument", "Webhook secret must be at least 8 bytes")

        webhook_id = f"wh_{secrets.token_hex(16)}"
        secret_hash = _token_hash(secret_ref)
        now = _utc_now()

        with self.store._transaction() as conn:
            conn.execute(
                """
                INSERT INTO bridge_webhooks(webhook_id, team_id, agent_id, url, secret_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (webhook_id, team_id, agent_id, url, secret_hash, now),
            )

        return {
            "webhook_id": webhook_id,
            "team_id": team_id,
            "agent_id": agent_id,
            "url": url,
            "active": True,
            "created_at": now,
        }

    def deliver(self, webhook_id: str, event: dict[str, Any], signing_secret: str | None = None) -> dict[str, Any]:
        """Deliver an event to a registered webhook with HMAC signature.

        If ``signing_secret`` is provided it is used as the HMAC key (the caller
        retrieves it from their secret store). Otherwise the stored SHA-256 hash
        is used as the signing key — this keeps the "hash only at rest" hygiene
        while still producing verifiable signatures in test/self-test contexts.
        """
        with self.store._read() as conn:
            row = conn.execute(
                "SELECT * FROM bridge_webhooks WHERE webhook_id = ? AND active = 1",
                (webhook_id,),
            ).fetchone()
        if row is None:
            raise FinalismaError("not_found", f"Webhook '{webhook_id}' not found or inactive")

        url = row["url"]
        secret_hash = row["secret_hash"]

        # Fail closed: the stored value is a SHA-256 HASH of the signing secret,
        # not the secret itself. Using it as the live HMAC key would let anyone
        # with DB read access forge valid webhook signatures. The caller must
        # supply the real secret from its own secret store.
        if signing_secret is None:
            raise FinalismaError(
                "signing_secret_required",
                "deliver requires the real webhook signing secret; the stored hash cannot be used as the signing key",
            )
        signing_key = signing_secret.encode("utf-8")

        timestamp = str(int(_now_epoch()))
        signature = _hmac_sign(signing_key, timestamp, event)

        body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Finalisma-Signature": signature,
                "X-Finalisma-Timestamp": timestamp,
                "User-Agent": "Finalisma-Webhook/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                status = resp.status
        except (urllib.error.URLError, OSError) as exc:
            # Record failure
            with self.store._transaction() as conn:
                conn.execute(
                    "UPDATE bridge_webhooks SET failure_count = failure_count + 1 WHERE webhook_id = ?",
                    (webhook_id,),
                )
            return {"delivered": False, "error": str(exc)}

        with self.store._transaction() as conn:
            conn.execute(
                "UPDATE bridge_webhooks SET last_delivered_at = ?, failure_count = 0 WHERE webhook_id = ?",
                (_utc_now(), webhook_id),
            )
        return {"delivered": True, "status": status, "webhook_id": webhook_id}

    @staticmethod
    def verify_signature(
        secret: str,
        signature: str,
        timestamp: str,
        body: Any,
        max_age_seconds: int = 300,
    ) -> bool:
        """Verify an HMAC-SHA256 webhook signature within a replay window."""
        try:
            ts = int(timestamp)
        except (ValueError, TypeError):
            return False
        now = int(_now_epoch())
        if abs(now - ts) > max_age_seconds:
            return False
        signed_payload = f"{timestamp}.{json.dumps(body, sort_keys=True, separators=(',', ':'))}"
        expected = "sha256=" + hmac_mod.new(
            secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac_mod.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# 2. PollingBridge
# ---------------------------------------------------------------------------

class PollingBridge:
    """Durable outbox-backed polling for hosts that cannot receive push.

    Events are delivered at-most-once: ack checkpoints the cursor and
    acknowledged events are not re-delivered.
    """

    def __init__(self, store: FinalismaStore) -> None:
        self.store = store
        init_bridge(store)

    def enqueue(
        self,
        team_id: str,
        agent_id: str,
        event: dict[str, Any],
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        """Add an event to an agent's outbox."""
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        _authorize_actor(self.store, team_id, agent_id, actor_token)

        event_id = f"bo_{secrets.token_hex(16)}"
        now = _utc_now()
        with self.store._transaction() as conn:
            # Assign next sequence monotonically per (team, agent)
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM bridge_outbox WHERE team_id = ? AND agent_id = ?",
                (team_id, agent_id),
            ).fetchone()
            next_seq = row["next_seq"]
            conn.execute(
                """
                INSERT INTO bridge_outbox(event_id, team_id, agent_id, event_json, seq, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, team_id, agent_id, json.dumps(event, ensure_ascii=False), next_seq, now),
            )
        return {"event_id": event_id, "seq": next_seq, "created_at": now}

    def get_pending(
        self,
        team_id: str,
        agent_id: str,
        cursor: int = 0,
        actor_token: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return unacked events for an agent since the given cursor."""
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        _authorize_actor(self.store, team_id, agent_id, actor_token)

        with self.store._read() as conn:
            rows = conn.execute(
                """
                SELECT event_id, event_json, seq, acked
                FROM bridge_outbox
                WHERE team_id = ? AND agent_id = ? AND seq > ? AND acked = 0
                ORDER BY seq ASC
                LIMIT ?
                """,
                (team_id, agent_id, cursor, limit),
            ).fetchall()

        events = []
        max_seq = cursor
        for row in rows:
            evt = json.loads(row["event_json"])
            evt["event_id"] = row["event_id"]
            evt["seq"] = row["seq"]
            evt["acked"] = bool(row["acked"])
            events.append(evt)
            max_seq = max(max_seq, row["seq"])

        return {
            "events": events,
            "next_cursor": max_seq,
            "has_more": len(events) >= limit,
        }

    def ack(
        self,
        team_id: str,
        agent_id: str,
        event_ids: Sequence[str],
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        """Acknowledge events and checkpoint the cursor."""
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        _authorize_actor(self.store, team_id, agent_id, actor_token)

        if not event_ids:
            raise FinalismaError("invalid_argument", "At least one event_id is required")

        now = _utc_now()
        with self.store._transaction() as conn:
            for eid in event_ids:
                conn.execute(
                    "UPDATE bridge_outbox SET acked = 1 WHERE event_id = ? AND team_id = ? AND agent_id = ?",
                    (eid, team_id, agent_id),
                )
            # Checkpoint cursor to max acked seq
            conn.execute(
                """
                INSERT INTO bridge_cursors(team_id, agent_id, last_ack_seq, updated_at)
                VALUES (?, ?, (SELECT COALESCE(MAX(seq), 0) FROM bridge_outbox WHERE team_id = ? AND agent_id = ? AND acked = 1), ?)
                ON CONFLICT(team_id, agent_id) DO UPDATE SET
                    last_ack_seq = excluded.last_ack_seq,
                    updated_at = excluded.updated_at
                """,
                (team_id, agent_id, team_id, agent_id, now),
            )
            row = conn.execute(
                "SELECT last_ack_seq FROM bridge_cursors WHERE team_id = ? AND agent_id = ?",
                (team_id, agent_id),
            ).fetchone()
        return {"ok": True, "acked_count": len(event_ids), "cursor": row["last_ack_seq"]}


# ---------------------------------------------------------------------------
# 3. ClipboardBridge
# ---------------------------------------------------------------------------

class ClipboardBridge:
    """CLI copy-paste tier: generates a one-shot bootstrap snippet.

    The snippet is a compact JSON containing endpoint, team, pairing link,
    and consent contract. Parsing is strict: rejects token-bearing URLs,
    enforces one-use via nonce tracking.
    """

    def __init__(self, store: FinalismaStore) -> None:
        self.store = store
        init_bridge(store)

    def generate_bootstrap(
        self,
        team_id: str,
        agent_id: str,
        endpoint: str,
        pairing_id: str,
        join_token: str,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        """Generate a one-shot bootstrap snippet for clipboard transfer."""
        _validate_id(team_id, "team_id")
        _validate_id(agent_id, "agent_id")
        _authorize_actor(self.store, team_id, agent_id, actor_token)

        if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
            raise FinalismaError("invalid_argument", "endpoint must be an http(s) URL")

        nonce = secrets.token_hex(16)
        now = _utc_now()
        with self.store._transaction() as conn:
            conn.execute(
                "INSERT INTO bridge_bootstrap_nonces(nonce, created_at) VALUES (?, ?)",
                (nonce, now),
            )

        return {
            "endpoint": endpoint,
            "team_id": team_id,
            "pairing_id": pairing_id,
            "join_token": join_token,
            "join_url": f"{endpoint}/v1/join/{pairing_id}#token={join_token}",
            "consent_contract": {
                "action": "consent_then_join",
                "requires": "consent=true (JSON boolean)",
                "preview_first": True,
            },
            "_nonce": nonce,
            "created_at": now,
        }

    def parse_bootstrap(self, raw: str) -> dict[str, Any]:
        """Parse and validate a bootstrap snippet. One-use enforced."""
        try:
            snippet = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FinalismaError("invalid_argument", "Bootstrap must be valid JSON") from exc

        if not isinstance(snippet, dict):
            raise FinalismaError("invalid_argument", "Bootstrap must be a JSON object")

        # Reject token-bearing URL paths
        join_url = snippet.get("join_url", "")
        if isinstance(join_url, str) and "/v1/join/" in join_url:
            path_part = join_url.split("/v1/join/")[1]
            # The path after /v1/join/ should be the pairing_id only (no token)
            pairing_part = path_part.split("#")[0]
            if not pairing_part.startswith("pair_"):
                raise FinalismaError(
                    "legacy_join_url_rejected",
                    "Join URLs must contain a public pairing ID; token in path is forbidden",
                )

        # Enforce one-use via nonce
        nonce = snippet.get("_nonce")
        if isinstance(nonce, str):
            with self.store._transaction() as conn:
                row = conn.execute(
                    "SELECT consumed_at FROM bridge_bootstrap_nonces WHERE nonce = ?",
                    (nonce,),
                ).fetchone()
                if row is None:
                    raise FinalismaError("invalid_argument", "Unknown or expired bootstrap nonce")
                if row["consumed_at"] is not None:
                    raise FinalismaError("bootstrap_reused", "Bootstrap snippet has already been consumed")
                conn.execute(
                    "UPDATE bridge_bootstrap_nonces SET consumed_at = ? WHERE nonce = ?",
                    (_utc_now(), nonce),
                )

        return snippet


# ---------------------------------------------------------------------------
# 4. HttpBridgeClient
# ---------------------------------------------------------------------------

class HttpBridgeClient:
    """Tiny stdlib http.client wrapper — the 'give a link' universal client.

    Lets ANY process (no MCP) perform: preview_link, join_link, send_event,
    poll_events, ack. URL fragment token handling exactly like core: token
    in #fragment, sent in POST body, never in path.
    """

    def __init__(self, base_url: str, timeout: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._connections: list[HTTPConnection] = []

    def _parse_url(self, url: str) -> tuple[str, int, str]:
        """Parse base_url into (host, port, scheme)."""
        from urllib.parse import urlsplit
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "http":
            raise FinalismaError("invalid_argument", "Only http is supported in this bridge client")
        port = parsed.port or 80
        return parsed.hostname or "127.0.0.1", port, parsed.path

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        host, port, _ = self._parse_url(self.base_url)
        import http.client
        conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        self._connections.append(conn)
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body is not None else None
        all_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            all_headers.update(headers)
        conn.request(method, path, body=payload, headers=all_headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        if resp.status >= 400:
            try:
                error_data = json.loads(resp_body)
                error = error_data.get("error", {})
                raise FinalismaError(error.get("code", "http_error"), error.get("message", f"HTTP {resp.status}"))
            except json.JSONDecodeError:
                raise FinalismaError("http_error", f"HTTP {resp.status}")
        if not resp_body:
            return {}
        return json.loads(resp_body)

    def _extract_pairing_and_token(self, join_url: str) -> tuple[str, str]:
        """Extract public pairing ID and fragment token from a join URL."""
        from urllib.parse import urlsplit
        fragment = urlsplit(join_url).fragment
        if not fragment.startswith("token="):
            raise FinalismaError("invalid_token", "Join URL must contain a #token= fragment")
        token = fragment[len("token="):]
        path = urlsplit(join_url).path
        if not path.startswith(("/v1/join/", "/join/")):
            raise FinalismaError("invalid_argument", "URL path must be a /v1/join/<id> or /join/<id> path")
        pairing_key = path.split("/")[-1]
        if not pairing_key.startswith("pair_"):
            raise FinalismaError("legacy_join_url_rejected", "Join URLs must contain a public pairing ID")
        return pairing_key, token

    def preview_link(self, join_url: str) -> dict[str, Any]:
        """Preview a pairing link (non-mutating)."""
        pairing_key, _ = self._extract_pairing_and_token(join_url)
        return self._request("GET", f"/v1/join/{pairing_key}")

    def join_link(
        self,
        join_url: str,
        agent_id: str,
        consent: Any,
        name: str | None = None,
        role: str = "generalist",
        capabilities: list[str] | None = None,
        actor_token: str | None = None,
    ) -> dict[str, Any]:
        """Join a pairing via link with explicit consent."""
        if consent is not True:
            raise FinalismaError("consent_required", "consent must be the JSON boolean true")
        pairing_key, token = self._extract_pairing_and_token(join_url)
        body = {
            "token": token,
            "agent_id": agent_id,
            "consent": True,
            "role": role,
        }
        if name:
            body["name"] = name
        if capabilities:
            body["capabilities"] = capabilities
        if actor_token:
            body["actor_token"] = actor_token
        return self._request("POST", f"/v1/join/{pairing_key}", body=body)

    def send_event(
        self,
        session_token: str,
        agent_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        actor_token: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a session event via the MCP JSON-RPC interface."""
        params = {
            "session_token": session_token,
            "agent_id": agent_id,
            "kind": kind,
            "payload": payload,
            "idempotency_key": idempotency_key,
        }
        if actor_token:
            params["actor_token"] = actor_token
        if trace_id:
            params["trace_id"] = trace_id
        return self._mcp_call("finalisma_session_send", params)

    def poll_events(
        self,
        session_token: str,
        agent_id: str,
        after_seq: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Poll session events after a cursor."""
        return self._mcp_call("finalisma_session_poll", {
            "session_token": session_token,
            "agent_id": agent_id,
            "after_seq": after_seq,
            "limit": limit,
        })

    def ack(self, session_token: str, agent_id: str, seq: int) -> dict[str, Any]:
        """Acknowledge events up to seq."""
        return self._mcp_call("finalisma_session_ack", {
            "session_token": session_token,
            "agent_id": agent_id,
            "seq": seq,
        })

    def _mcp_call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Make a tools/call JSON-RPC request."""
        response = self._request("POST", "/mcp", body={
            "jsonrpc": "2.0",
            "id": secrets.token_hex(8),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        result = response.get("result", {})
        if result.get("isError"):
            error_text = result["content"][0]["text"]
            try:
                error_data = json.loads(error_text)
                error = error_data.get("error", {})
                raise FinalismaError(error.get("code", "tool_error"), error.get("message", "Tool call failed"))
            except json.JSONDecodeError:
                raise FinalismaError("tool_error", error_text)
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        # Parse text content
        content = result.get("content", [])
        for item in content:
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    pass
        return {}

    def close(self) -> None:
        for conn in self._connections:
            try:
                conn.close()
            except Exception:
                pass
        self._connections.clear()


# ---------------------------------------------------------------------------
# Module-level UTC helper (mirrors core's private _utc_now)
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
