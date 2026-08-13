"""Weft Web App — browser front-end over the cloud service.

The human-facing surface over the SAME CloudRoomService + identity plane
the agent-facing HTTP service (service.py) drives. Signup/login, org
management, room dashboard, and the connect-an-agent page.

Contract: WeftWebApp(backend, static_dir=..., state_dir=...) exposes
.handler (a BaseHTTPRequestHandler subclass) and is driven on
("127.0.0.1", 0) with finally teardown.

Stdlib-only. No templates engine — HTML is rendered via string
composition with mandatory escaping so no raw token or password ever
appears in rendered output.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import ssl
import time as _time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from weft_cloud.identity import (
    AccountStore,
    AgentKeyStore,
    AuthError,
    InviteStore,
    OrgStore,
    RoleError,
    SessionContext,
    SessionStore,
)
from weft_cloud.identity.accounts import signup as _identity_signup
from weft_cloud.identity.accounts import verify_email as _identity_verify
from weft_cloud.identity.accounts import request_password_reset as _identity_reset_request
from weft_cloud.identity.accounts import reset_password as _identity_reset_password
from weft_cloud.identity.accounts import authenticate as _identity_authenticate
from weft_cloud.identity.mailer import smtp_config_from_env as _smtp_config_from_env
from weft_cloud.identity.accounts import burn_scrypt_cost as _identity_burn_scrypt_cost
from weft_cloud.identity.schema import ensure_schema as _ensure_identity_schema
from weft_cloud.identity.tokens import hash_token as _hash_token
from weft_cloud.quotas import QuotaError
from weft_cloud.rate_limit import RateLimitedError, enforce_auth_rate_limit
from weft_cloud.rooms import CloudRoomService, RoomError, _parse_json, public_origin
from weft_cloud.storage import StorageBackend
from weft_cloud.web.config_gen import CLIENTS, build_config
from weft_cloud.web.copy import connect_page_body
from weft_cloud.web.security_headers import security_headers

SESSION_COOKIE = "fss_session"
CSRF_COOKIE = "fss_csrf"
_MIN_PASSWORD_LEN = 8
_INVITE_PATH_RE = re.compile(r"^/invite/([A-Za-z0-9_-]+)$")
_ROOM_PATH_RE = re.compile(r"^/room/([A-Za-z0-9_-]+)$")
_ROOM_EVENTS_RE = re.compile(r"^/room/([A-Za-z0-9_-]+)/events$")
_ROOM_AUDIT_RE = re.compile(r"^/room/([A-Za-z0-9_-]+)/audit$")
_ROOM_CONNECT_RE = re.compile(r"^/room/([A-Za-z0-9_-]+)/connect$")
_ROOM_CLOSE_RE = re.compile(r"^/room/([A-Za-z0-9_-]+)/close$")
_ROOM_REVOKE_LINK_RE = re.compile(r"^/room/([A-Za-z0-9_-]+)/revoke-link$")

# Exact paths served to UNAUTHENTICATED callers. This is a fixed allowlist of
# specific strings — deliberately never a prefix or wildcard rule — so a typo
# or a future path can never broaden "public" to an authenticated route.
# Crawlers and uptime monitors must reach these without a session; everything
# else still passes through the auth gate below.
_PUBLIC_GET_PATHS = frozenset({
    "/terms.html",
    "/privacy.html",
    "/robots.txt",
    "/sitemap.xml",
    "/favicon.ico",
})

# Exact static files whose Content-Type must not be left to platform
# mimetypes: a crawler-facing type has to be correct on every host OS.
_STATIC_CONTENT_TYPE = {
    "/robots.txt": "text/plain; charset=utf-8",
    "/sitemap.xml": "application/xml; charset=utf-8",
}


class _WebError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message


def _esc(value: Any) -> str:
    """HTML-escaping helper. Never render unescaped user-controlled data."""
    return html.escape(str(value) if value is not None else "")


def _format_last_used(ts: Any) -> str:
    """Human-readable last-used timestamp; ``never`` when the key was unused."""
    if not ts:
        return "never"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (ValueError, OSError, OverflowError, TypeError):
        return "unknown"


def _format_iso(ts: Any) -> str:
    """Human-readable UTC time from an ISO-8601 string (``...Z``) or epoch."""
    if not ts:
        return ""
    text = str(ts)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError, OSError, OverflowError):
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        except (ValueError, TypeError, OSError, OverflowError):
            return text


def _new_csrf() -> str:
    return secrets.token_urlsafe(32)


# Minimal shared styling for the authenticated pages. The web app has no CSS
# system (stdlib-only, string-composed HTML); this tiny block keeps the
# dashboard readable without introducing one. CSP allows inline style.
_DASH_CSS = (
    '<style>'
    'body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;'
    'max-width:56rem;margin:0 auto;padding:1.5rem;line-height:1.45;color:#1a1a1a;}'
    'h1{font-size:1.5rem;margin:0 0 .25rem;}h2{font-size:1.15rem;margin-top:1.5rem;}'
    'table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem;}'
    'th,td{text-align:left;padding:.4rem .55rem;border-bottom:1px solid #e5e5e5;vertical-align:top;}'
    'th{font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;color:#666;}'
    'pre{background:#f6f6f4;border:1px solid #e0e0dd;padding:.75rem;overflow-x:auto;font-size:.8rem;}'
    'code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;}'
    'label{display:block;margin:.4rem 0;}'
    'input[type=text],input[type=number],input[type=password],input[type=email],select{'
    'padding:.35rem;margin-left:.25rem;border:1px solid #ccc;border-radius:3px;}'
    'button{padding:.35rem .75rem;margin:.15rem;border:1px solid #999;border-radius:3px;'
    'background:#fff;cursor:pointer;}'
    'button[type=submit]{background:#111;color:#fff;border-color:#111;}'
    '.muted{color:#777;}.flash{border:1px solid #d0d0cc;background:#fafaf8;'
    'padding:.5rem .75rem;margin:.5rem 0;}'
    '.flash-error{border-color:#c44;background:#fdf0f0;color:#8b1a1a;}'
    '.warn{border:1px solid #e0b400;background:#fff7d6;padding:.5rem .75rem;margin:.5rem 0;}'
    '.revoked{color:#8b1a1a;font-weight:600;}.active-ok{color:#1a7f37;}'
    '.bar{display:inline-block;height:.6rem;width:8rem;background:#e6e6e6;'
    'border-radius:3px;vertical-align:middle;margin-left:.4rem;}'
    '.bar>span{display:block;height:100%;background:#111;border-radius:3px;}'
    '</style>'
)

# Copy-to-clipboard control (progressive enhancement). Without JS the value
# remains selectable text; with JS the button copies. Feature-detected and
# exception-safe so a browser without the Clipboard API still works.
_COPY_JS = (
    '<script>'
    'function wfCopy(el){'
    'var t=el.getAttribute("data-copy")||"";'
    'function done(){el.textContent="Copied";}'
    'if(navigator.clipboard&&navigator.clipboard.writeText){'
    'navigator.clipboard.writeText(t).then(done,function(){done();});'
    '}else{'
    'var ta=document.createElement("textarea");ta.value=t;'
    'document.body.appendChild(ta);ta.select();'
    'try{document.execCommand("copy");}catch(e){}'
    'document.body.removeChild(ta);done();'
    '}'
    '}'
    '</script>'
)


def _copy_button(data_copy: str, label: str = "Copy") -> str:
    """Button that copies ``data_copy`` to the clipboard when JS is available."""
    return (
        f'<button type="button" data-copy="{_esc(data_copy)}" '
        f'onclick="wfCopy(this)">{_esc(label)}</button>'
    )


def _resolve_cookie_secure_flag() -> bool | None:
    """Operator override for the ``Secure`` cookie attribute.

    Unset -> None (auto-detect from the request). ``1``/``true``/``yes``/``on``
    forces Secure on, ``0``/``false``/``no``/``off`` forces it off. The auto
    path is right for plain local dev (http://127.0.0.1 must not set Secure)
    and for a TLS-terminating proxy (``X-Forwarded-Proto``); the flag exists
    for the deployment where neither is reliable.
    """
    raw = os.environ.get("WEFT_WEB_SECURE_COOKIES", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def _request_is_secure(handler: BaseHTTPRequestHandler) -> bool:
    """True when the request reached us over https.

    The live deployment terminates TLS in nginx and proxies plain http to this
    app, so we trust the proxy-standard ``X-Forwarded-Proto`` header; direct
    in-app TLS (a future option) is detected from the socket.
    """
    forwarded = (handler.headers.get("X-Forwarded-Proto", "") or "").lower()
    if forwarded == "https":
        return True
    sock = getattr(handler, "request", None)
    return isinstance(sock, ssl.SSLSocket)


# ---------------------------------------------------------------------------
# HTML helpers — all use double-quoted HTML attributes (the contract tests
# assert on `name="email"` etc.). Python strings are single-quoted so the
# double quotes inside are literal.
# ---------------------------------------------------------------------------

def _input(name: str, type_: str = "text", **attrs: str) -> str:
    extra = "".join(f' {k}="{_esc(v)}"' for k, v in attrs.items())
    return f'<input name="{_esc(name)}" type="{_esc(type_)}"{extra}>'


def _csrf_input(token: str) -> str:
    return f'<input type="hidden" name="_csrf" value="{_esc(token)}">'


def _label(text: str, control: str) -> str:
    return f'<label>{_esc(text)} {control}</label>'


def _page(title: str, body_html: str, *, csrf_token: str | None = None,
          extra_head: str = "") -> bytes:
    csrf_meta = ""
    if csrf_token:
        csrf_meta = f'<meta name="csrf-token" content="{_esc(csrf_token)}">'
    html_doc = (
        '<!DOCTYPE html>\n'
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{_esc(title)}</title>{csrf_meta}{extra_head}</head>\n'
        f'<body>{body_html}</body></html>'
    )
    return html_doc.encode("utf-8")


class WeftWebApp:
    """Browser front-end over the cloud identity + room planes."""

    def __init__(self, backend: StorageBackend, static_dir: str = "", state_dir: str = "",
                 *, smtp_configured: bool | None = None,
                 auth_rate_limits: dict | None = None) -> None:
        self.backend = backend
        self.static_dir = static_dir
        self.state_dir = state_dir
        self.auth_rate_limits = auth_rate_limits
        # "Email can actually be delivered" is a deployment property read from
        # the same environment the outbox worker uses. When unset we derive it
        # here so the web app self-corrects as soon as SMTP credentials land;
        # tests pass an explicit value to assert both branches.
        if smtp_configured is None:
            try:
                smtp_configured = _smtp_config_from_env() is not None
            except ValueError:
                smtp_configured = False
        self.smtp_configured = bool(smtp_configured)
        self.accounts = AccountStore(backend)
        self.sessions = SessionStore(backend)
        self.agent_keys = AgentKeyStore(backend)
        self.orgs = OrgStore(backend)
        self.invites = InviteStore(backend)
        self.rooms = CloudRoomService(backend)
        self._link_token_cache: dict[str, str] = {}
        self.secure_cookies = _resolve_cookie_secure_flag()
        _ensure_identity_schema(backend)
        self.rooms._ensure_room_schema()
        self.handler = _build_handler(self)

    # ------------------------------------------------------------------
    # Session / cookie helpers
    # ------------------------------------------------------------------

    def _cookie_secure(self, handler: BaseHTTPRequestHandler) -> bool:
        if self.secure_cookies is not None:
            return self.secure_cookies
        return _request_is_secure(handler)

    def _read_cookie(self, handler: BaseHTTPRequestHandler, name: str) -> str | None:
        raw = handler.headers.get("Cookie", "")
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == name:
                    return v.strip()
        return None

    def _session_context(self, handler: BaseHTTPRequestHandler) -> SessionContext | None:
        raw = self._read_cookie(handler, SESSION_COOKIE)
        if not raw:
            return None
        try:
            return self.sessions.validate(self.backend, raw)
        except AuthError:
            return None

    def _set_session_cookie(self, handler: BaseHTTPRequestHandler, raw_token: str) -> None:
        secure = "; Secure" if self._cookie_secure(handler) else ""
        handler.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={raw_token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age=86400{secure}",
        )

    def _clear_session_cookie(self, handler: BaseHTTPRequestHandler) -> None:
        secure = "; Secure" if self._cookie_secure(handler) else ""
        handler.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}",
        )

    def _set_csrf_cookie(self, handler: BaseHTTPRequestHandler, token: str) -> None:
        secure = "; Secure" if self._cookie_secure(handler) else ""
        handler.send_header(
            "Set-Cookie",
            f"{CSRF_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400{secure}",
        )

    def _read_csrf_cookie(self, handler: BaseHTTPRequestHandler) -> str | None:
        return self._read_cookie(handler, CSRF_COOKIE)

    # ------------------------------------------------------------------
    # CSRF validation
    # ------------------------------------------------------------------

    def _validate_csrf(self, handler: BaseHTTPRequestHandler, form: dict) -> None:
        submitted = form.get("_csrf", "")
        expected = self._read_csrf_cookie(handler)
        if not expected or not submitted or not secrets.compare_digest(submitted, expected):
            raise _WebError(HTTPStatus.FORBIDDEN, "CSRF validation failed")

    # ------------------------------------------------------------------
    # Auth gate
    # ------------------------------------------------------------------

    def _require_auth(self, handler: BaseHTTPRequestHandler) -> SessionContext:
        ctx = self._session_context(handler)
        if ctx is None:
            self._redirect(handler, "/login")
            raise _WebError(HTTPStatus.SEE_OTHER, "redirecting")
        return ctx

    # ------------------------------------------------------------------
    # HTTP response helpers
    # ------------------------------------------------------------------

    def _send_security_headers(self, handler: BaseHTTPRequestHandler, *,
                               html: bool = False) -> None:
        """Emit the shared security header block (see web/security_headers.py).

        ``html=True`` also sends the Content-Security-Policy; CSP is scoped to
        HTML responses on purpose.
        """
        for name, value in security_headers(html=html):
            handler.send_header(name, value)

    def _redirect(self, handler: BaseHTTPRequestHandler, location: str) -> None:
        handler.send_response(HTTPStatus.SEE_OTHER)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler)
        handler.end_headers()

    def _send_html(self, handler: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def _send_json(self, handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler)
        handler.end_headers()
        handler.wfile.write(body)

    # ------------------------------------------------------------------
    # Form parsing
    # ------------------------------------------------------------------

    def _discard_request_body(self, handler: BaseHTTPRequestHandler, max_bytes: int = 1_048_576) -> None:
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return
        handler.rfile.read(min(length, max_bytes))

    def _read_form(self, handler: BaseHTTPRequestHandler, max_bytes: int = 1_048_576) -> dict[str, str]:
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > max_bytes:
            return {}
        raw = handler.rfile.read(length).decode("utf-8", errors="replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    # ------------------------------------------------------------------
    # Internal lookups
    # ------------------------------------------------------------------

    def _email_exists(self, email: str) -> bool:
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT 1 FROM cloud_identity_accounts WHERE email = ?",
                (email,),
            ).fetchone()
        return row is not None

    def _tenant_for_email(self, email: str) -> str | None:
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT tenant_id FROM cloud_identity_accounts WHERE email = ? ORDER BY created_at DESC LIMIT 1",
                (email,),
            ).fetchone()
        return row["tenant_id"] if row else None

    def _role_for_account(self, tenant_id: str, account_id: str) -> str | None:
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT role FROM cloud_identity_members WHERE tenant_id = ? AND account_id = ?",
                (tenant_id, account_id),
            ).fetchone()
        return row["role"] if row else None

    def _account_email(self, account_id: str) -> str | None:
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT email FROM cloud_identity_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return row["email"] if row else None

    def _list_members(self, ctx: SessionContext) -> list[dict]:
        try:
            return self.orgs.list_members(ctx)
        except RoleError:
            return []

    def _membership_count(self, account_id: str) -> int:
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT COUNT(*) AS c FROM cloud_identity_members WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return row["c"] if row else 0

    def _list_rooms_for_account(self, ctx: SessionContext) -> list[dict]:
        return self.rooms.list_rooms_for_member(ctx.tenant_id, ctx.account_id)

    def _room_member_counts(self, tenant_id: str) -> dict[str, int]:
        """Active member count per room in a tenant (one tenant-scoped query)."""
        counts: dict[str, int] = {}
        with self.backend.transaction() as tx:
            rows = tx.execute(
                "SELECT room_id, COUNT(*) AS c FROM cloud_room_members "
                "WHERE tenant_id = ? AND status = 'active' GROUP BY room_id",
                (tenant_id,),
            ).fetchall()
            for r in rows:
                counts[r["room_id"]] = int(r["c"])
        return counts

    def _plan_usage(self, ctx: SessionContext) -> dict:
        """Plan id + live usage, read from the SAME quota plane the API enforces.

        ``PLANS`` (via ``resolve_plan``) is the single source of truth for the
        limits and the ``cloud_counters`` / ``cloud_room_counters`` tables are
        the counters the quota gates increment — never a second copy.
        """
        from weft_cloud.quotas import events_month_bucket, resolve_plan

        plan_id, plan = resolve_plan(self.backend, ctx.tenant_id)
        with self.backend.transaction() as tx:
            rooms_row = tx.execute(
                "SELECT value FROM cloud_counters WHERE tenant_id = ? AND counter = 'rooms'",
                (ctx.tenant_id,),
            ).fetchone()
            events_row = tx.execute(
                "SELECT value FROM cloud_counters WHERE tenant_id = ? AND counter = ?",
                (ctx.tenant_id, events_month_bucket()),
            ).fetchone()
            member_row = tx.execute(
                "SELECT COALESCE(MAX(value), 0) AS m FROM cloud_room_counters "
                "WHERE tenant_id = ? AND counter = 'members'",
                (ctx.tenant_id,),
            ).fetchone()
        return {
            "plan_id": plan_id,
            "rooms_used": int(rooms_row["value"]) if rooms_row else 0,
            "max_rooms": plan.max_rooms,
            "events_used": int(events_row["value"]) if events_row else 0,
            "max_events_per_month": plan.max_events_per_month,
            "largest_room_members": int(member_row["m"]) if member_row else 0,
            "max_members_per_room": plan.max_members_per_room,
        }

    def _plan_usage_html(self, ctx: SessionContext) -> str:
        """Render the plan + usage strip against the real enforced limits."""
        usage = self._plan_usage(ctx)

        def _bar(used: int, limit: int) -> str:
            pct = 0 if limit <= 0 else max(0, min(100, int(100 * used / limit)))
            return f'<span class="bar"><span style="width:{pct}%"></span></span>'

        return (
            '<div class="flash">'
            f'<strong>Plan: {_esc(usage["plan_id"])}</strong> — '
            f'Rooms {usage["rooms_used"]}/{usage["max_rooms"]}'
            f'{_bar(usage["rooms_used"], usage["max_rooms"])} · '
            f'Members per room max {usage["max_members_per_room"]}'
            f' (largest room: {usage["largest_room_members"]}) · '
            f'Events this month {usage["events_used"]}/{usage["max_events_per_month"]}'
            f'{_bar(usage["events_used"], usage["max_events_per_month"])}'
            '</div>'
        )

    def _room_belongs_to_tenant(self, room_id: str, tenant_id: str) -> bool:
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT 1 FROM cloud_rooms WHERE room_id = ? AND tenant_id = ?",
                (room_id, tenant_id),
            ).fetchone()
        return row is not None

    def _room_connect_entitled(self, tenant_id: str, room_id: str,
                               account_id: str) -> bool:
        """True only if ``account_id`` may receive this room's raw link token.

        The ``rm_`` link token is a multi-use bearer capability that admits
        anyone to the room, so the caller must be the room's OWNER or an ACTIVE
        MEMBER of THIS specific room. Org membership alone is NOT enough.

        This deliberately returns False for both "the room does not exist in
        this tenant" and "the room exists but the caller is neither owner nor
        member", so the refusal cannot be used as a room-existence oracle.
        """
        with self.backend.transaction() as tx:
            room = tx.execute(
                "SELECT owner_agent_id FROM cloud_rooms "
                "WHERE tenant_id = ? AND room_id = ?",
                (tenant_id, room_id),
            ).fetchone()
            if room is None:
                return False
            member = tx.execute(
                "SELECT 1 FROM cloud_room_members "
                "WHERE tenant_id = ? AND room_id = ? AND agent_id = ? "
                "AND status = 'active'",
                (tenant_id, room_id, account_id),
            ).fetchone()
            return room["owner_agent_id"] == account_id or member is not None

    def _poll_for_member(self, tenant_id: str, room_id: str, after_seq: int,
                         agent_id: str) -> dict:
        """Event poll for an org member (doesn't require room membership).

        ``agent_id`` is the VIEWER's identity: every payload is routed through
        ``_filter_payload_for_agent`` exactly as the /v1 poll does, so a
        viewer who is not the addressee of a unicast sees the redacted
        envelope, never the private body.
        """
        with self.backend.transaction() as tx:
            room = tx.execute(
                "SELECT * FROM cloud_rooms WHERE tenant_id = ? AND room_id = ?",
                (tenant_id, room_id),
            ).fetchone()
            if room is None:
                raise RoomError("room_not_found", "Room not found", 404)
            rows = tx.execute(
                "SELECT * FROM cloud_room_event_log WHERE tenant_id = ? AND room_id = ? AND seq > ? "
                "ORDER BY seq ASC LIMIT 100",
                (tenant_id, room_id, after_seq),
            ).fetchall()
            events = []
            next_seq = int(room["cursor_head"]) + 1
            for r in rows:
                events.append({
                    "event_id": r["event_id"],
                    "seq": r["seq"],
                    "origin_agent": r["origin_agent"],
                    "kind": r["kind"],
                    "payload": self.rooms._filter_payload_for_agent(
                        _parse_json(r["payload_json"], {}), agent_id),
                    "created_at": r["created_at"],
                })
                next_seq = r["seq"] + 1
            return {
                "room_id": room_id,
                "state": room["state"],
                "events": events,
                "next_seq": next_seq,
                "cursor_head": int(room["cursor_head"]),
                "last_ack_seq": 0,
                "has_more": False,
            }

    def _room_info_for_member(self, tenant_id: str, room_id: str,
                              agent_id: str | None = None) -> dict:
        """Room info for an org member (doesn't require room membership).

        When ``agent_id`` is the room OWNER, the result additionally carries
        the link control surface: ``link_id`` (what revoke needs) and
        ``link_revoked`` (whether a revocation landed). Other members never
        see them — a non-owner who cannot revoke has no reason to know the
        link identifier, and least exposure wins.
        """
        with self.backend.transaction() as tx:
            room = tx.execute(
                "SELECT * FROM cloud_rooms WHERE tenant_id = ? AND room_id = ?",
                (tenant_id, room_id),
            ).fetchone()
            if room is None:
                raise RoomError("room_not_found", "Room not found", 404)
            members = tx.execute(
                "SELECT m.agent_id, m.status, m.joined_at, a.email "
                "FROM cloud_room_members m "
                "LEFT JOIN cloud_identity_accounts a ON a.account_id = m.agent_id "
                "WHERE m.tenant_id = ? AND m.room_id = ? AND m.status = 'active' "
                "ORDER BY m.joined_at",
                (tenant_id, room_id),
            ).fetchall()
            member_list = []
            for m in members:
                member_list.append({
                    "agent_id": m["agent_id"],
                    "email": m["email"],
                    "status": m["status"],
                    "joined_at": m["joined_at"],
                })
            result = {
                "room_id": room_id,
                "name": room["name"],
                "state": room["state"],
                "cap": room["cap"],
                "member_count": len(member_list),
                "members": member_list,
                "owner_agent_id": room["owner_agent_id"],
                "created_at": room["created_at"],
            }
            if agent_id is not None and room["owner_agent_id"] == agent_id:
                link = tx.execute(
                    "SELECT link_id, revoked FROM cloud_room_links "
                    "WHERE tenant_id = ? AND room_id = ? LIMIT 1",
                    (tenant_id, room_id),
                ).fetchone()
                if link is not None:
                    result["link_id"] = link["link_id"]
                    result["link_revoked"] = bool(link["revoked"])
            return result

    def _event_log_for_member(self, tenant_id: str, room_id: str, agent_id: str) -> list[dict]:
        """Event log for an org member (doesn't require room membership).

        Payloads are redacted for the VIEWER (``agent_id``) exactly as
        ``_poll_for_member`` does — a viewer who is not the addressee of a
        unicast never sees its body.
        """
        with self.backend.transaction() as tx:
            room = tx.execute(
                "SELECT 1 FROM cloud_rooms WHERE tenant_id = ? AND room_id = ?",
                (tenant_id, room_id),
            ).fetchone()
            if room is None:
                raise RoomError("room_not_found", "Room not found", 404)
            rows = tx.execute(
                "SELECT * FROM cloud_room_event_log WHERE tenant_id = ? AND room_id = ? ORDER BY seq",
                (tenant_id, room_id),
            ).fetchall()
        events = []
        for r in rows:
            events.append({
                "event_id": r["event_id"],
                "seq": r["seq"],
                "origin_agent": r["origin_agent"],
                "kind": r["kind"],
                "payload": self.rooms._filter_payload_for_agent(
                    _parse_json(r["payload_json"], {}), agent_id),
                "created_at": r["created_at"],
            })
        return events

    def _get_room_link_token(self, room_id: str) -> str | None:
        return self._link_token_cache.get(room_id)

    def _store_link_token(self, room_id: str, raw_token: str) -> None:
        self._link_token_cache[room_id] = raw_token

    # ------------------------------------------------------------------
    # Route handlers — public pre-auth
    # ------------------------------------------------------------------

    def handle_get_health(self, handler: BaseHTTPRequestHandler) -> None:
        """Public liveness endpoint (GET /health).

        200 without auth so a load balancer / uptime monitor reports the
        service UP while it is up — a health check behind a login redirect
        is useless. The body is deliberately minimal and public: no version
        numbers, build hashes, database paths, or dependency versions.
        """
        self._send_json(handler, HTTPStatus.OK, {"status": "ok"})

    def handle_get_signup(self, handler: BaseHTTPRequestHandler) -> None:
        token = _new_csrf()
        form = (
            '<h1>Sign up</h1>'
            '<form method="post" action="/signup">'
            f'{_csrf_input(token)}'
            f'{_label("Email", _input("email", "email", required="required"))}'
            f'{_label("Password", _input("password", "password", required="required"))}'
            '<button type="submit">Create account</button>'
            '</form>'
            '<p>By creating an account you agree to the '
            '<a href="/terms.html">Terms of Service</a> and '
            '<a href="/privacy.html">Privacy Policy</a>.</p>'
            '<p>Already have an account? <a href="/login">Log in</a></p>'
        )
        body = _page("Sign up", form, csrf_token=token)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, token)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def handle_post_signup(self, handler: BaseHTTPRequestHandler) -> None:
        form = self._read_form(handler)
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""
        if len(password) < _MIN_PASSWORD_LEN or not email:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Sign up failed",
                                  '<p>Invalid email or password too short.</p>'
                                  '<p><a href="/signup">Try again</a></p>'))
            return
        # Public endpoint: each signup mints a tenant + writes rows, so the
        # shared auth limiter runs BEFORE any account work. Keyed on the client
        # IP and the requested email; the refusal is identical for any email.
        enforce_auth_rate_limit(self.backend, handler, "signup", email=email,
                                limits=self.auth_rate_limits)
        # Check for duplicate email across all tenants BEFORE creating one.
        if self._email_exists(email):
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Sign up failed",
                                  '<p>Could not create account — email may already be in use.</p>'
                                  '<p><a href="/signup">Try again</a></p>'))
            return
        tenant_id = f"tenant_{secrets.token_hex(8)}"
        try:
            account_id, _raw_vt = _identity_signup(self.backend, tenant_id, email, password)
        except Exception:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Sign up failed",
                                  '<p>Could not create account — email may already be in use.</p>'
                                  '<p><a href="/signup">Try again</a></p>'))
            return
        # Signup creates an org (tenant) whose owner is the signing-up user.
        # The identity signup only creates the account + tenant rows; the
        # owner membership row is the web app's bootstrap responsibility.
        from weft_cloud.storage import utc_now_iso as _utc
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_members(tenant_id, account_id, role, joined_at) "
                "VALUES (?, ?, 'owner', ?)",
                (tenant_id, account_id, _utc()),
            )
            tx.commit()
        self._redirect(handler, "/login?verify_sent=1")

    def handle_get_login(self, handler: BaseHTTPRequestHandler, *, error: str | None = None) -> None:
        token = _new_csrf()
        error_html = ""
        if error:
            error_html = f'<div class="flash flash-error">{_esc(error)}</div>'
        notice_html = self._login_notice_html(handler)
        form = (
            f'<h1>Log in</h1>{error_html}{notice_html}'
            '<form method="post" action="/login">'
            f'{_csrf_input(token)}'
            f'{_label("Email", _input("email", "email", required="required"))}'
            f'{_label("Password", _input("password", "password", required="required"))}'
            '<button type="submit">Log in</button>'
            '</form>'
            '<p><a href="/reset-request">Forgot password?</a></p>'
            '<p><a href="/signup">Create an account</a></p>'
        )
        body = _page("Log in", form, csrf_token=token)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, token)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def _login_notice_html(self, handler: BaseHTTPRequestHandler) -> str:
        """Flash message on the login page, driven by whether SMTP is configured.

        ``verify_sent`` / ``reset_sent`` redirect targets imply an email was
        sent. When SMTP delivery is NOT configured that implication is false —
        the message sits in the outbox and is never delivered — so the page
        says so plainly instead of pretending. When credentials land, this
        self-corrects: the wording switches back to the normal "check your
        email" copy. Never reveals a verification or reset token or link.
        """
        params = parse_qs(urlsplit(handler.path).query)
        if "verify_sent" in params:
            if self.smtp_configured:
                text = "Check your email — a verification link was sent to the address you signed up with."
            else:
                text = ("Email delivery is not enabled yet, so the verification message could not be sent. "
                        "Your account still works — you can log in now without verifying.")
            return f'<div class="flash">{_esc(text)}</div>'
        if "reset_sent" in params:
            if self.smtp_configured:
                text = "Check your email — a password reset link was sent."
            else:
                text = ("Email delivery is not enabled yet, so a password-reset message cannot be sent. "
                        "Contact the operator to reset your password.")
            return f'<div class="flash">{_esc(text)}</div>'
        if "verified" in params:
            return '<div class="flash">Your account is verified — you can log in now.</div>'
        if "reset_done" in params:
            return '<div class="flash">Your password was reset — you can log in now.</div>'
        return ""

    def handle_post_login(self, handler: BaseHTTPRequestHandler) -> None:
        form = self._read_form(handler)
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""
        # Enforce the shared auth limiter BEFORE the tenant lookup, keyed on
        # the client IP and the email (counted whether or not the account
        # exists), so throttling cannot reveal whether an email is registered.
        enforce_auth_rate_limit(self.backend, handler, "signin", email=email,
                                limits=self.auth_rate_limits)
        tenant_id = self._tenant_for_email(email)
        if tenant_id is None:
            # Timing parity: an unknown email must cost the same scrypt work as
            # a wrong password on a known email, or login becomes a
            # user-enumeration timing oracle. Burn the cost, then show the
            # identical refusal page.
            _identity_burn_scrypt_cost(password)
            self.handle_get_login(handler, error="Invalid email or password.")
            return
        try:
            account_id = _identity_authenticate(self.backend, tenant_id, email, password)
        except AuthError:
            self.handle_get_login(handler, error="Invalid email or password.")
            return
        role = self._role_for_account(tenant_id, account_id)
        if role is None:
            # Do not mint a session for an account removed from this tenant.
            self.handle_get_login(handler, error="Invalid email or password.")
            return
        session_id, raw_token = self.sessions.create(
            self.backend, tenant_id, account_id, role
        )
        handler.send_response(HTTPStatus.SEE_OTHER)
        handler.send_header("Location", "/")
        self._set_session_cookie(handler, raw_token)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler)
        handler.end_headers()

    def handle_post_verify(self, handler: BaseHTTPRequestHandler) -> None:
        form = self._read_form(handler)
        token = form.get("token", "")
        if not token:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Verification failed",
                                  '<p>Missing verification token.</p>'))
            return
        try:
            _identity_verify(self.backend, token)
            self._redirect(handler, "/login?verified=1")
        except AuthError:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Verification failed",
                                  '<p>Invalid or expired verification link.</p>'))

    def handle_get_verify(self, handler: BaseHTTPRequestHandler) -> None:
        qs = urlsplit(handler.path).query
        params = parse_qs(qs)
        token = (params.get("token", [""])[0])
        if not token:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Verification failed",
                                  '<p>Missing verification token.</p>'))
            return
        # GET must NEVER mutate state: link scanners, antivirus and mail-client
        # prefetchers fetch GET URLs automatically, which would "verify" the
        # address without a human ever clicking. So GET only renders a
        # confirmation page; the actual verification happens on the POST below
        # (POST /verify), which prefetchers do not issue. This also matches the
        # reset flow (GET renders the form, POST performs the action).
        csrf = _new_csrf()
        form = (
            '<h1>Confirm your email</h1>'
            '<p>Click the button below to confirm that this email address '
            'belongs to you. This is the only step that verifies your email.</p>'
            '<form method="post" action="/verify">'
            f'{_csrf_input(csrf)}'
            f'<input type="hidden" name="token" value="{_esc(token)}">'
            '<button type="submit">Confirm email address</button>'
            '</form>'
            '<p><a href="/login">Back to log in</a></p>'
        )
        body = _page("Confirm your email", form, csrf_token=csrf)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, csrf)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def handle_get_reset_request(self, handler: BaseHTTPRequestHandler) -> None:
        token = _new_csrf()
        form = (
            '<h1>Reset password</h1>'
            '<form method="post" action="/reset-request">'
            f'{_csrf_input(token)}'
            f'{_label("Email", _input("email", "email", required="required"))}'
            '<button type="submit">Send reset link</button>'
            '</form>'
            '<p><a href="/login">Back to login</a></p>'
        )
        body = _page("Reset password", form, csrf_token=token)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, token)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def handle_post_reset_request(self, handler: BaseHTTPRequestHandler) -> None:
        form = self._read_form(handler)
        email = (form.get("email") or "").strip()
        if email:
            # Public endpoint and a mail-bomb vector now that real delivery is
            # live: the shared auth limiter runs on the email REGARDLESS of
            # whether the account exists, so a throttled unknown address gets
            # the identical 429 as a throttled known one (no existence oracle).
            enforce_auth_rate_limit(self.backend, handler, "reset_request",
                                    email=email, limits=self.auth_rate_limits)
            tenant_id = self._tenant_for_email(email)
            if tenant_id:
                _identity_reset_request(self.backend, tenant_id, email)
        self._redirect(handler, "/login?reset_sent=1")

    def handle_get_reset(self, handler: BaseHTTPRequestHandler) -> None:
        qs = urlsplit(handler.path).query
        params = parse_qs(qs)
        token = (params.get("token", [""])[0])
        if not token:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Reset failed", '<p>Missing reset token.</p>'))
            return
        csrf = _new_csrf()
        form = (
            '<h1>Reset password</h1>'
            '<form method="post" action="/reset">'
            f'{_csrf_input(csrf)}'
            f'<input type="hidden" name="token" value="{_esc(token)}">'
            f'{_label("New password", _input("password", "password", required="required"))}'
            '<button type="submit">Reset password</button>'
            '</form>'
        )
        body = _page("Reset password", form, csrf_token=csrf)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, csrf)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def handle_post_reset(self, handler: BaseHTTPRequestHandler) -> None:
        form = self._read_form(handler)
        token = form.get("token", "")
        password = form.get("password", "")
        if len(password) < _MIN_PASSWORD_LEN or not token:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Reset failed",
                                  '<p>Invalid token or password too short.</p>'))
            return
        try:
            _identity_reset_password(self.backend, token, password)
            self._redirect(handler, "/login?reset_done=1")
        except AuthError:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Reset failed",
                                  '<p>Invalid or expired reset token.</p>'))

    def handle_post_logout(self, handler: BaseHTTPRequestHandler) -> None:
        # If not authenticated, just redirect to login (no crash).
        ctx = self._session_context(handler)
        if ctx is None:
            self._redirect(handler, "/login")
            return
        form = self._read_form(handler)
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        raw = self._read_cookie(handler, SESSION_COOKIE)
        if raw:
            try:
                token_hash = _hash_token(raw)
                with self.backend.transaction() as tx:
                    row = tx.execute(
                        "SELECT session_id FROM cloud_identity_sessions WHERE token_hash = ?",
                        (token_hash,),
                    ).fetchone()
                if row:
                    from weft_cloud.identity.sessions import revoke as _revoke
                    _revoke(self.backend, row["session_id"])
            except AuthError:
                pass
        handler.send_response(HTTPStatus.SEE_OTHER)
        handler.send_header("Location", "/login")
        self._clear_session_cookie(handler)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler)
        handler.end_headers()

    # ------------------------------------------------------------------
    # Dashboard (root)
    # ------------------------------------------------------------------

    def handle_get_root(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        csrf = _new_csrf()
        email = self._account_email(ctx.account_id) or ""
        rooms = self._list_rooms_for_account(ctx)
        counts = self._room_member_counts(ctx.tenant_id)

        rows_html = ""
        for r in rooms:
            room_id = r["room_id"]
            member_count = counts.get(room_id, 0)
            state = r["state"]
            created = _format_last_used(r["created_at"])
            rows_html += (
                f'<tr><td><a href="/room/{_esc(room_id)}">'
                f'{_esc(r["name"] or room_id)}</a></td>'
                f'<td>{member_count}/{_esc(r["cap"])}</td>'
                f'<td>{_esc(state)}</td>'
                f'<td class="muted">{_esc(created)}</td></tr>'
            )
        if not rows_html:
            rows_html = '<tr><td colspan="4" class="muted">No rooms yet.</td></tr>'

        body_html = (
            '<h1>Dashboard</h1>'
            f'<p>Logged in as {_esc(email)} ({_esc(ctx.role)})</p>'
            f'{self._plan_usage_html(ctx)}'
            '<h2>Rooms</h2>'
            '<table><thead><tr><th>Room</th><th>Members / cap</th>'
            '<th>State</th><th>Created</th></tr></thead>'
            f'<tbody>{rows_html}</tbody></table>'
            '<h2>Create a room</h2>'
            '<form method="post" action="/rooms">'
            f'{_csrf_input(csrf)}'
            f'{_label("Room name", _input("name", "text", required="required"))}'
            f'{_label("Cap (max members)", _input("cap", "number", value="8", min="2", max="64"))}'
            '<button type="submit">Create room</button>'
            '</form>'
            '<p>After creating a room you land on its page with the shareable '
            'join link. The link is a <strong>credential</strong>: anyone who '
            'holds it can join the room, from any tenant.</p>'
            '<h2>Connect a client</h2>'
            '<p><a href="/config">Generate a connector config</a> for Claude '
            'Desktop, Codex or Cursor — a complete, working stdio MCP config '
            'with a fresh agent key already embedded.</p>'
            '<h2>Account</h2>'
            '<p><a href="/agent-keys">Agent keys</a> · '
            '<a href="/org">Organization</a></p>'
            '<form method="post" action="/logout">'
            f'{_csrf_input(csrf)}'
            '<button type="submit">Log out</button>'
            '</form>'
        )
        body = _page("Dashboard", body_html, csrf_token=csrf, extra_head=_DASH_CSS + _COPY_JS)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, csrf)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    # ------------------------------------------------------------------
    # Org routes
    # ------------------------------------------------------------------

    def handle_get_org(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        csrf = _new_csrf()
        members = self._list_members(ctx)
        rows_html = ""
        for m in members:
            rows_html += (
                f'<tr><td>{_esc(m["email"])}</td><td>{_esc(m["role"])}</td>'
                f'<td>{_esc(m.get("account_id", ""))}</td></tr>'
            )
        body_html = (
            '<h1>Organization</h1>'
            '<table><thead><tr><th>Email</th><th>Role</th><th>ID</th></tr></thead>'
            f'<tbody>{rows_html}</tbody></table>'
            '<h2>Invite</h2>'
            '<form method="post" action="/org/invite">'
            f'{_csrf_input(csrf)}'
            f'{_label("Email", _input("email", "email", required="required"))}'
            '<label>Role <select name="role">'
            '<option value="member">member</option>'
            '<option value="admin">admin</option>'
            '</select></label>'
            '<button type="submit">Invite</button>'
            '</form>'
            '<h2>Role</h2>'
            '<form method="post" action="/org/role">'
            f'{_csrf_input(csrf)}'
            f'{_label("Account ID", _input("account_id", "text", required="required"))}'
            '<label>Role <select name="role">'
            '<option value="member">member</option>'
            '<option value="admin">admin</option>'
            '<option value="owner">owner</option>'
            '</select></label>'
            '<button type="submit">Set role</button>'
            '</form>'
            '<h2>Remove</h2>'
            '<form method="post" action="/org/remove">'
            f'{_csrf_input(csrf)}'
            f'{_label("Account ID", _input("account_id", "text", required="required"))}'
            '<button type="submit">Remove</button>'
            '</form>'
            '<h2>Leave</h2>'
            '<form method="post" action="/org/leave">'
            f'{_csrf_input(csrf)}'
            '<button type="submit">Leave organization</button>'
            '</form>'
            '<p><a href="/">Back to dashboard</a></p>'
        )
        body = _page("Organization", body_html, csrf_token=csrf)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, csrf)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def handle_post_org(self, handler: BaseHTTPRequestHandler) -> None:
        """POST /org — create a new org. Refused if already in one."""
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        form = self._read_form(handler)
        # One-org-per-account check fires BEFORE CSRF (design §9.2 #15).
        if self._membership_count(ctx.account_id) > 0:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Cannot create org",
                                  '<p>Your account already belongs to an organization.</p>'))
            return
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        self._send_html(handler, HTTPStatus.BAD_REQUEST,
                        _page("Cannot create org",
                              '<p>Your account already belongs to an organization.</p>'))

    def handle_post_org_invite(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        form = self._read_form(handler)
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        email = (form.get("email") or "").strip()
        role = form.get("role", "member")
        if not email or role not in ("admin", "member"):
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Invite failed", '<p>Invalid email or role.</p>'))
            return
        try:
            self.invites.create(ctx, email, role)
        except (AuthError, RoleError):
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>Not allowed.</p>'))
            return
        self._redirect(handler, "/org")

    def handle_get_invite(self, handler: BaseHTTPRequestHandler, token: str) -> None:
        csrf = _new_csrf()
        form = (
            '<h1>Accept invite</h1>'
            f'<form method="post" action="/invite/{_esc(token)}">'
            f'{_csrf_input(csrf)}'
            f'{_label("Email", _input("email", "email", required="required"))}'
            f'{_label("Password", _input("password", "password", required="required"))}'
            '<button type="submit">Accept invite</button>'
            '</form>'
        )
        body = _page("Accept invite", form, csrf_token=csrf)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, csrf)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def handle_post_invite(self, handler: BaseHTTPRequestHandler, token: str) -> None:
        form = self._read_form(handler)
        email = (form.get("email") or "").strip()
        password = form.get("password", "")
        if len(password) < _MIN_PASSWORD_LEN or not email:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Invite failed",
                                  '<p>Invalid email or password too short.</p>'))
            return
        try:
            account_id, session_token = self.invites.accept(self.backend, token, email, password)
        except AuthError:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Invite failed",
                                  '<p>Invalid or expired invite.</p>'))
            return
        handler.send_response(HTTPStatus.SEE_OTHER)
        handler.send_header("Location", "/")
        self._set_session_cookie(handler, session_token)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler)
        handler.end_headers()

    def handle_post_org_role(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        form = self._read_form(handler)
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        account_id = form.get("account_id", "")
        new_role = form.get("role", "")
        if not account_id or new_role not in ("member", "admin", "owner"):
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Role change failed",
                                  '<p>Invalid account or role.</p>'))
            return
        try:
            self.orgs.set_role(ctx, account_id, new_role)
        except RoleError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>Not allowed.</p>'))
            return
        except (AuthError, ValueError):
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Role change failed",
                                  '<p>Invalid request.</p>'))
            return
        self._redirect(handler, "/org")

    def handle_post_org_remove(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        form = self._read_form(handler)
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        account_id = form.get("account_id", "")
        if not account_id:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Remove failed",
                                  '<p>Account ID required.</p>'))
            return
        try:
            self.orgs.remove_member(ctx, account_id)
        except RoleError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>Not allowed.</p>'))
            return
        self._redirect(handler, "/org")

    def handle_post_org_leave(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        form = self._read_form(handler)
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        if ctx.role == "owner":
            members = self._list_members(ctx)
            if len(members) > 1:
                self._send_html(handler, HTTPStatus.BAD_REQUEST,
                                _page("Cannot leave",
                                      '<p>Owner cannot leave while other members exist.</p>'))
                return
        self._redirect(handler, "/org")

    # ------------------------------------------------------------------
    # Agent-key routes — session-cookie-gated (the interactive identity plane;
    # a leaked agk_ bearer credential must never manage keys).
    # ------------------------------------------------------------------

    def handle_get_agent_keys(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        csrf = _new_csrf()
        keys = self.agent_keys.list_for_account(self.backend, ctx.tenant_id, ctx.account_id)
        rows_html = ""
        for k in keys:
            key_id = k["key_id"]
            revoked = k["revoked_at"] is not None
            if revoked:
                status_html = '<span class="revoked">revoked</span>'
                action_html = '<span class="muted">revoked</span>'
            else:
                status_html = '<span class="active-ok">active</span>'
                action_html = (
                    f'<form method="post" action="/agent-keys/revoke" style="display:inline">'
                    f'{_csrf_input(csrf)}'
                    f'<input type="hidden" name="key_id" value="{_esc(key_id)}">'
                    '<button type="submit" '
                    'onclick="return confirm(\'Revoke this agent key? It will stop '
                    'working immediately and cannot be un-revoked.\')">Revoke</button>'
                    '</form>'
                )
            rows_html += (
                f'<tr><td>{_esc(k["label"])}</td>'
                f'<td>{_esc(_format_last_used(k["created_at"]))}</td>'
                f'<td>{_esc(_format_last_used(k["last_used_at"]))}</td>'
                f'<td>{status_html}</td>'
                f'<td>{action_html}</td></tr>'
            )
        if not rows_html:
            rows_html = '<tr><td colspan="5" class="muted">No agent keys yet.</td></tr>'
        body_html = (
            '<h1>Agent keys</h1>'
            '<p>Long-lived credentials for MCP client configs. An agent key never '
            'expires and never signs in; it stays valid until you revoke it. The raw '
            'key is shown <strong>once</strong> at creation and stored only as a hash.</p>'
            '<p class="warn"><strong>Key management is session-only.</strong> An '
            '<code>agk_</code> key can use the room surface but can never mint, list '
            'or revoke keys — those actions require your interactive session.</p>'
            '<h2>Create a key</h2>'
            '<form method="post" action="/agent-keys">'
            f'{_csrf_input(csrf)}'
            f'{_label("Label", _input("label", "text", required="required", placeholder="e.g. Claude Desktop"))}'
            '<button type="submit">Create key</button>'
            '</form>'
            '<h2>Your keys</h2>'
            '<table><thead><tr><th>Label</th><th>Created</th><th>Last used</th>'
            '<th>Status</th><th></th></tr></thead>'
            f'<tbody>{rows_html}</tbody></table>'
            '<p><a href="/config">Generate a connector config</a> · '
            '<a href="/">Back to dashboard</a></p>'
        )
        body = _page("Agent keys", body_html, csrf_token=csrf, extra_head=_DASH_CSS + _COPY_JS)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, csrf)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def handle_post_agent_keys(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        form = self._read_form(handler)
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        label = (form.get("label") or "").strip()[:64] or "default"
        key_id, raw_token = self.agent_keys.create(
            self.backend, ctx.tenant_id, ctx.account_id, label
        )
        # The raw token is rendered EXACTLY ONCE, in this response body. It is
        # never stored, redirected through a query string, or retrievable again.
        body_html = (
            '<h1>Agent key created</h1>'
            '<p class="warn"><strong>You will not see this key again.</strong> '
            'It is shown below exactly once and stored only as a hash. If you '
            'lose it, create a new key and revoke this one.</p>'
            f'<pre><code>{_esc(raw_token)}</code></pre>'
            f'{_copy_button(raw_token)}'
            f'<p>Put it in your MCP client config as <code>WEFT_TOKEN</code> under the '
            f'<code>weft</code> server entry — or use the '
            f'<a href="/config">connector config generator</a>, which builds the '
            f'whole config for you. It never expires; revoke it here when you '
            'stop using it.</p>'
            f'<p><a href="/agent-keys">Manage agent keys</a> · '
            f'<a href="/">Back to dashboard</a></p>'
        )
        body = _page("Agent key created", body_html, extra_head=_DASH_CSS + _COPY_JS)
        self._send_html(handler, HTTPStatus.OK, body)

    def handle_post_agent_keys_revoke(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        form = self._read_form(handler)
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        key_id = (form.get("key_id") or "").strip()
        if key_id:
            self.agent_keys.revoke(self.backend, ctx.tenant_id, ctx.account_id, key_id)
        self._redirect(handler, "/agent-keys")

    # ------------------------------------------------------------------
    # Connector-config generator (session-gated)
    # ------------------------------------------------------------------

    def _weft_mcp_script_path(self) -> str:
        """Absolute path to the stdio bridge script shipped with the product.

        The generated configs launch this script on the user's machine, so the
        path is resolved from the installed package, not guessed.
        """
        return str(Path(__file__).resolve().parents[3] / "scripts" / "weft-mcp.py")

    def handle_get_config(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        csrf = _new_csrf()
        origin = public_origin()
        options = ""
        for cid, meta in CLIENTS.items():
            options += (
                f'<option value="{_esc(cid)}">{_esc(meta["label"])}</option>'
            )
        body_html = (
            '<h1>Connector config generator</h1>'
            '<p>Generate a complete, working stdio MCP config for your client '
            'with a <strong>freshly minted agent key already embedded</strong>. '
            'The config launches the Weft stdio bridge (<code>weft-mcp.py '
            '--remote … --token-env WEFT_TOKEN</code>) so your client reaches '
            'the hosted rooms — these clients speak stdio MCP '
            '(<code>command</code> + <code>args</code>), not an HTTP '
            '<code>url</code>.</p>'
            '<p>Hosted endpoint: <code>{}</code></p>'.format(_esc(origin))
            + '<form method="post" action="/config">'
            + _csrf_input(csrf)
            + '<label>Client <select name="client" required="required">'
            + options
            + '</select></label>'
            + '<button type="submit">Generate config</button>'
            + '</form>'
            + '<p class="warn"><strong>This embeds a live credential.</strong> '
            'The generated config contains a real <code>agk_</code> agent key. '
            'Anyone who gets the config can act as that agent, and revoking the '
            'key invalidates the config. Treat it like a password.</p>'
            + '<p><a href="/">Back to dashboard</a></p>'
        )
        body = _page("Connector config", body_html, csrf_token=csrf,
                     extra_head=_DASH_CSS)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, csrf)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def handle_post_config(self, handler: BaseHTTPRequestHandler) -> None:
        """POST /config — mint a fresh agent key and render its config once.

        The raw ``agk_`` key appears in this response EXACTLY ONCE (embedded in
        the config), matching the agent-key contract. A new key is minted on
        every generation; the old one stays listed on /agent-keys so it can be
        revoked when the config is retired.
        """
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        form = self._read_form(handler)
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        client = (form.get("client") or "").strip()
        if client not in CLIENTS:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Config failed",
                                  '<p>Unknown client. Choose Claude Desktop, '
                                  'Cursor or Codex.</p>'))
            return
        label = f"connector:{client}"[:64]
        _, raw_token = self.agent_keys.create(
            self.backend, ctx.tenant_id, ctx.account_id, label
        )
        try:
            config = build_config(
                client,
                agent_key=raw_token,
                origin=public_origin(),
                script_path=self._weft_mcp_script_path(),
            )
        except ValueError as exc:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Config failed", f"<p>{_esc(str(exc))}</p>"))
            return
        meta = CLIENTS[client]
        body_html = (
            '<h1>Connector config — {}</h1>'.format(_esc(meta["label"]))
            + '<p class="warn"><strong>This config embeds a live credential.</strong> '
            'The <code>WEFT_TOKEN</code> value is a real, freshly minted '
            '<code>agk_</code> agent key. Anyone who gets this file can act as '
            'that agent, and <strong>revoking the key invalidates the '
            'config</strong>. It is shown here exactly once.</p>'
            + '<h2>Install</h2>'
            + f'<p>Save this as <code>{_esc(config["file"])}</code> and restart '
            'your client. The token is in the <code>env</code> block — never in '
            '<code>args</code> (argv is visible to every process on the '
            'machine). The <code>PYTHONUTF8=1</code> entry is required on '
            'Windows: without it the client&#39;s UTF-8 JSON-RPC is decoded as '
            'cp1252 and every non-ASCII character is destroyed.</p>'
            + '<h2>Config</h2>'
            + f'<pre><code>{_esc(config["config_text"])}</code></pre>'
            + _copy_button(config["config_text"], "Copy config")
            + f'<p>Bridge: <code>{_esc(config["origin"])}/mcp</code>. The room '
            'tools (<code>room_create</code>, <code>room_join</code>, '
            '<code>room_send</code>, <code>room_poll</code>, '
            '<code>room_wait</code>, …) appear in your client once connected.</p>'
            + '<p><a href="/agent-keys">Manage agent keys</a> · '
            '<a href="/config">Generate another</a> · '
            '<a href="/">Back to dashboard</a></p>'
        )
        body = _page("Connector config", body_html, extra_head=_DASH_CSS + _COPY_JS)
        self._send_html(handler, HTTPStatus.OK, body)

    # ------------------------------------------------------------------
    # Room routes
    # ------------------------------------------------------------------

    def handle_post_rooms(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        form = self._read_form(handler)
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        name = (form.get("name") or "").strip()
        try:
            cap = int(form.get("cap", "8"))
        except ValueError:
            cap = 8
        if not name:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Create room failed",
                                  '<p>Room name required.</p>'))
            return
        try:
            ctx.require_role("admin")
        except RoleError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden",
                                  '<p>Only admins can create rooms.</p>'))
            return
        try:
            result = self.rooms.create_room(
                tenant_id=ctx.tenant_id,
                owner_agent_id=ctx.account_id,
                actor_token=secrets.token_urlsafe(32),
                cap=cap,
                name=name,
            )
        except QuotaError as exc:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Create room failed", f"<p>{_esc(str(exc))}</p>"))
            return
        room_id = result["room_id"]
        self._store_link_token(room_id, result["link_token"])
        self._redirect(handler, f"/room/{room_id}")

    def handle_get_rooms(self, handler: BaseHTTPRequestHandler) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        csrf = _new_csrf()
        rooms = self._list_rooms_for_account(ctx)
        items_html = ""
        for r in rooms:
            items_html += (
                f'<li><a href="/room/{_esc(r["room_id"])}">{_esc(r["name"] or r["room_id"])}</a>'
                f' <span class="muted">({_esc(r["state"])})</span></li>'
            )
        if not items_html:
            items_html = '<li class="muted">No rooms yet.</li>'
        body_html = (
            '<h1>Rooms</h1>'
            f'<ul>{items_html}</ul>'
            '<form method="post" action="/rooms">'
            f'{_csrf_input(csrf)}'
            f'{_label("Room name", _input("name", "text", required="required"))}'
            f'{_label("Cap", _input("cap", "number", value="8", min="2", max="64"))}'
            '<button type="submit">Create room</button>'
            '</form>'
            '<p><a href="/">Back to dashboard</a></p>'
        )
        body = _page("Rooms", body_html, csrf_token=csrf)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, csrf)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def handle_get_room_detail(self, handler: BaseHTTPRequestHandler, room_id: str) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        if not self._room_belongs_to_tenant(room_id, ctx.tenant_id):
            self._send_html(handler, HTTPStatus.NOT_FOUND,
                            _page("Not found", '<p>Room not found.</p>'))
            return
        csrf = _new_csrf()
        # View is member+ (org member can view any room in their org). The raw
        # link token and the revoke control are shown only where the caller is
        # entitled (owner or active member of THIS room) — never on a page a
        # non-member can reach.
        try:
            info = self._room_info_for_member(ctx.tenant_id, room_id, ctx.account_id)
            events = self._event_log_for_member(ctx.tenant_id, room_id, ctx.account_id)
        except RoomError as exc:
            if exc.status == 404:
                self._send_html(handler, HTTPStatus.NOT_FOUND,
                                _page("Not found", '<p>Room not found.</p>'))
            else:
                self._send_html(handler, HTTPStatus.FORBIDDEN,
                                _page("Forbidden",
                                      '<p>Not a member of this room.</p>'))
            return

        is_owner = info["owner_agent_id"] == ctx.account_id
        entitled = self._room_connect_entitled(ctx.tenant_id, room_id, ctx.account_id)

        roster_html = ""
        for m in info["members"]:
            label = m.get("email") or m["agent_id"]
            status_html = (
                f'<span class="active-ok">{_esc(m["status"])}</span>'
                if m["status"] == "active" else _esc(m["status"])
            )
            roster_html += (
                f'<li>{_esc(label)} — {status_html} '
                f'<span class="muted">joined {_esc(_format_iso(m["joined_at"]))}</span></li>'
            )
        if not roster_html:
            roster_html = '<li class="muted">No members yet.</li>'

        events_html = ""
        for e in events[-50:]:
            events_html += (
                f'<li><span class="seq">#{_esc(e["seq"])}</span> '
                f'{_esc(e["kind"])} <span class="muted">from '
                f'{_esc(e["origin_agent"])} at '
                f'{_esc(_format_iso(e.get("created_at")))}</span></li>'
            )
        if not events_html:
            events_html = '<li class="muted">No events yet.</li>'

        # Link control surface — owner and active members only.
        link_section = ""
        if entitled:
            revoked = info.get("link_revoked", False)
            if revoked:
                link_section = (
                    '<div class="warn"><strong>Join link revoked.</strong> This '
                    'link no longer admits anyone. Create a new room (or, if '
                    're-enabling is desired, contact support) — revocation is '
                    'permanent for this link.</div>'
                )
            else:
                raw_token = self._get_room_link_token(room_id) or ""
                if raw_token:
                    shareable = f"{public_origin()}/j/{raw_token}"
                    link_section = (
                        '<div class="flash">'
                        f'<strong>Shareable join link</strong>: '
                        f'<code>{_esc(shareable)}</code> {_copy_button(shareable)}'
                        '<p class="warn"><strong>The link is a credential.</strong> '
                        'Anyone holding it can join this room from any tenant. '
                        'Share it only with the agents you intend to admit, and '
                        'revoke it the moment it is leaked or no longer needed.</p>'
                        '</div>'
                    )
                else:
                    link_section = (
                        '<div class="flash"><strong>Join link.</strong> The raw '
                        'link token is held only in the session that created the '
                        'room (it is stored as a hash, never raw). It is shown on '
                        'this page immediately after creation, and on the '
                        '<a href="'
                        f'/room/{_esc(room_id)}/connect">connect</a> page.</div>'
                    )
            if is_owner and info.get("link_id"):
                revoke_form = (
                    '<form method="post" '
                    f'action="/room/{_esc(room_id)}/revoke-link">'
                    f'{_csrf_input(csrf)}'
                    f'<input type="hidden" name="link_id" value="{_esc(info["link_id"])}">'
                    '<button type="submit">Revoke join link</button>'
                    '</form>'
                )
                link_section += revoke_form

        close_form = ""
        if is_owner:
            close_form = (
                f'<form method="post" action="/room/{_esc(room_id)}/close">'
                f'{_csrf_input(csrf)}'
                '<button type="submit">Close room</button>'
                '</form>'
            )

        body_html = (
            f'<h1>{_esc(info.get("name") or room_id)}</h1>'
            f'<p>State: <strong>{_esc(info["state"])}</strong> · '
            f'Members {info["member_count"]}/{_esc(info["cap"])} · '
            f'Created {_esc(_format_iso(info.get("created_at")))}</p>'
            f'{link_section}'
            '<h2>Members</h2>'
            f'<ul>{roster_html}</ul>'
            '<h2>Recent events</h2>'
            f'<ol>{events_html}</ol>'
            f'<p><a href="/room/{_esc(room_id)}/connect">Connect an agent</a></p>'
            f'{close_form}'
            f'<p><a href="/">Back to dashboard</a> · '
            f'<a href="/room/{_esc(room_id)}/audit">Audit log</a></p>'
        )
        body = _page(_esc(info.get("name") or room_id), body_html,
                     csrf_token=csrf, extra_head=_DASH_CSS + _COPY_JS)
        handler.send_response(HTTPStatus.OK)
        self._set_csrf_cookie(handler, csrf)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        self._send_security_headers(handler, html=True)
        handler.end_headers()
        handler.wfile.write(body)

    def handle_post_room_revoke_link(self, handler: BaseHTTPRequestHandler,
                                     room_id: str) -> None:
        """POST /room/{room_id}/revoke-link — revoke the room's join link (owner).

        Surfaces the REAL result: ``CloudRoomService.revoke_link`` raises
        ``link_not_found`` (404) when no link actually flipped — it cannot
        report success while revoking nothing — and ``owner_required`` (403)
        for a non-owner. Both are rendered back to the user with their true
        status; a failed revoke never looks successful.
        """
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        if not self._room_belongs_to_tenant(room_id, ctx.tenant_id):
            self._send_html(handler, HTTPStatus.NOT_FOUND,
                            _page("Not found", '<p>Room not found.</p>'))
            return
        form = self._read_form(handler)
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        link_id = (form.get("link_id") or "").strip()
        if not link_id:
            self._send_html(handler, HTTPStatus.BAD_REQUEST,
                            _page("Revoke failed",
                                  '<p>Missing link id.</p>'))
            return
        try:
            result = self.rooms.revoke_link(ctx.tenant_id, room_id,
                                            ctx.account_id, link_id)
        except RoomError as exc:
            if exc.status == 404:
                self._send_html(handler, HTTPStatus.NOT_FOUND,
                                _page("Revoke failed",
                                      '<p>The link could not be revoked — it '
                                      'was not found (it may already be '
                                      'revoked). Nothing changed.</p>'))
            else:
                self._send_html(handler, HTTPStatus.FORBIDDEN,
                                _page("Revoke failed",
                                      '<p>Only the room owner can revoke the '
                                      'join link. Nothing was revoked.</p>'))
            return
        self._redirect(handler, f"/room/{room_id}")

    def handle_get_room_events(self, handler: BaseHTTPRequestHandler, room_id: str) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        if not self._room_belongs_to_tenant(room_id, ctx.tenant_id):
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        qs = urlsplit(handler.path).query
        params = parse_qs(qs)
        after_seq_str = (params.get("after_seq", ["0"])[0])
        try:
            after_seq = int(after_seq_str)
        except ValueError:
            after_seq = 0
        # Event polling for org members — read-only, tenant-scoped.
        try:
            result = self._poll_for_member(ctx.tenant_id, room_id, after_seq, ctx.account_id)
        except RoomError as exc:
            if exc.status == 404:
                self._send_json(handler, HTTPStatus.NOT_FOUND,
                                {"error": "not_found"})
            else:
                self._send_json(handler, HTTPStatus.FORBIDDEN,
                                {"error": "forbidden"})
            return
        safe_events = []
        for e in result["events"]:
            safe_events.append({
                "seq": e["seq"],
                "kind": e["kind"],
                "origin": e["origin_agent"],
                "payload": e.get("payload"),
                "created_at": e.get("created_at"),
            })
        self._send_json(handler, HTTPStatus.OK, {
            "events": safe_events,
            "next_seq": result["next_seq"],
        })

    def handle_get_room_audit(self, handler: BaseHTTPRequestHandler, room_id: str) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        if not self._room_belongs_to_tenant(room_id, ctx.tenant_id):
            self._send_html(handler, HTTPStatus.NOT_FOUND,
                            _page("Not found", '<p>Room not found.</p>'))
            return
        try:
            events = self._event_log_for_member(ctx.tenant_id, room_id, ctx.account_id)
        except RoomError as exc:
            if exc.status == 404:
                self._send_html(handler, HTTPStatus.NOT_FOUND,
                                _page("Not found", '<p>Room not found.</p>'))
            else:
                self._send_html(handler, HTTPStatus.FORBIDDEN,
                                _page("Forbidden", '<p>Not a member.</p>'))
            return
        items_html = ""
        for e in events:
            items_html += (
                f'<li><span class="seq">#{_esc(e["seq"])}</span> '
                f'{_esc(e["kind"])} <span class="muted">from '
                f'{_esc(e["origin_agent"])}</span></li>'
            )
        body_html = (
            '<h1>Audit log</h1>'
            f'<ol>{items_html}</ol>'
            f'<p><a href="/room/{_esc(room_id)}">Back to room</a></p>'
        )
        body = _page("Audit log", body_html)
        self._send_html(handler, HTTPStatus.OK, body)

    def handle_get_room_connect(self, handler: BaseHTTPRequestHandler, room_id: str) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        # The page returns the room's raw rm_ link token — a multi-use bearer
        # capability that admits anyone to the room. Only the room's OWNER or
        # an ACTIVE MEMBER of THIS room may receive it. The caller's identity
        # comes from the authenticated session, never from the request.
        # A caller who is not entitled (including a same-org member who was
        # never in the room) gets the IDENTICAL 404 as a room that does not
        # exist, so the endpoint is not an existence oracle.
        if not self._room_connect_entitled(ctx.tenant_id, room_id, ctx.account_id):
            self._send_html(handler, HTTPStatus.NOT_FOUND,
                            _page("Not found", '<p>Room not found.</p>'))
            return
        link_token = self._get_room_link_token(room_id) or ""
        body_html = (
            connect_page_body(room_id, link_token)
            + f'<p><a href="/room/{_esc(room_id)}">Back to room</a></p>'
        )
        body = _page("Connect an agent", body_html)
        self._send_html(handler, HTTPStatus.OK, body)

    def handle_post_room_close(self, handler: BaseHTTPRequestHandler, room_id: str) -> None:
        ctx = self._require_auth(handler)
        if ctx is None:
            return
        if not self._room_belongs_to_tenant(room_id, ctx.tenant_id):
            self._send_html(handler, HTTPStatus.NOT_FOUND,
                            _page("Not found", '<p>Room not found.</p>'))
            return
        form = self._read_form(handler)
        try:
            self._validate_csrf(handler, form)
        except _WebError:
            self._send_html(handler, HTTPStatus.FORBIDDEN,
                            _page("Forbidden", '<p>CSRF validation failed.</p>'))
            return
        try:
            self.rooms.close_room(ctx.tenant_id, room_id, ctx.account_id)
        except RoomError as exc:
            if exc.status == 404:
                self._send_html(handler, HTTPStatus.NOT_FOUND,
                                _page("Not found", '<p>Room not found.</p>'))
            else:
                self._send_html(handler, HTTPStatus.FORBIDDEN,
                                _page("Forbidden",
                                      '<p>Only the room owner can close it.</p>'))
            return
        self._redirect(handler, f"/room/{room_id}")


# ---------------------------------------------------------------------------
# HTTP handler factory
# ---------------------------------------------------------------------------

def _build_handler(app: WeftWebApp) -> type[BaseHTTPRequestHandler]:
    """Build the HTTP handler class bound to the given app instance."""

    class _WebHTTPHandler(BaseHTTPRequestHandler):
        server_version = "weft-web/0.1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _handle_request(self) -> None:
            try:
                self._dispatch()
            except _WebError:
                pass
            except RateLimitedError as exc:
                # A static, identical page for every throttled request (the
                # limiter runs before any existence-dependent branch, so this
                # body can never vary between a known and an unknown email).
                try:
                    retry_after = str(int(max(1.0, exc.retry_after or 1.0)))
                    body = (
                        b'<!DOCTYPE html>'
                        b'<html lang="en"><head><meta charset="utf-8">'
                        b'<meta name="viewport" content="width=device-width,initial-scale=1">'
                        b'<title>Too many requests</title></head>'
                        b'<body><h1>Too many requests</h1>'
                        b'<p>Slow down and try again shortly.</p>'
                        b'<p><a href="/login">Back to log in</a></p>'
                        b'</body></html>'
                    )
                    self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Retry-After", retry_after)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    pass
            except Exception:
                try:
                    self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", "21")
                    self.send_header("Cache-Control", "no-store")
                    app._send_security_headers(self)
                    self.end_headers()
                    self.wfile.write(b"Internal server error")
                except Exception:
                    pass

        def _dispatch(self) -> None:
            path = urlsplit(self.path).path
            method = self.command

            # --- Public pre-auth routes (GET) ---
            if method == "GET" and path == "/signup":
                app.handle_get_signup(self)
                return
            if method == "GET" and path == "/login":
                app.handle_get_login(self)
                return
            if method == "GET" and path == "/verify":
                app.handle_get_verify(self)
                return
            if method == "GET" and path == "/reset":
                app.handle_get_reset(self)
                return
            if method == "GET" and path == "/reset-request":
                app.handle_get_reset_request(self)
                return
            if method == "GET" and path in _PUBLIC_GET_PATHS:
                # Exact public static allowlist (terms/privacy/robots/sitemap/
                # favicon). When no static bundle is mounted these must 404 —
                # never redirect a crawler or icon request to the login page.
                if app.static_dir:
                    self._serve_static(path)
                else:
                    app._send_json(self, HTTPStatus.NOT_FOUND,
                                   {"error": {"code": "not_found",
                                              "message": "Not found"}})
                return
            if method == "GET" and path == "/health":
                app.handle_get_health(self)
                return

            # --- Public pre-auth routes (POST) ---
            if method == "POST" and path == "/signup":
                app.handle_post_signup(self)
                return
            if method == "POST" and path == "/login":
                app.handle_post_login(self)
                return
            if method == "POST" and path == "/verify":
                app.handle_post_verify(self)
                return
            if method == "POST" and path == "/reset-request":
                app.handle_post_reset_request(self)
                return
            if method == "POST" and path == "/reset":
                app.handle_post_reset(self)
                return

            # --- Logout (POST) — CSRF-gated; redirects if not authed ---
            if method == "POST" and path == "/logout":
                app.handle_post_logout(self)
                return

            # --- Invite accept (public) ---
            invite_match = _INVITE_PATH_RE.match(path)
            if invite_match:
                token = invite_match.group(1)
                if method == "GET":
                    app.handle_get_invite(self, token)
                elif method == "POST":
                    app.handle_post_invite(self, token)
                return

            # --- Everything below requires auth ---
            ctx = app._session_context(self)
            if ctx is None:
                if method == "POST":
                    app._discard_request_body(self)
                # Clear any stale session cookie and redirect to login.
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/login")
                app._clear_session_cookie(self)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                app._send_security_headers(self)
                self.end_headers()
                return

            # --- Authenticated routes ---
            if method == "GET" and path == "/":
                app.handle_get_root(self)
                return
            if method == "GET" and path == "/org":
                app.handle_get_org(self)
                return
            if method == "POST" and path == "/org":
                app.handle_post_org(self)
                return
            if method == "POST" and path == "/org/invite":
                app.handle_post_org_invite(self)
                return
            if method == "POST" and path == "/org/role":
                app.handle_post_org_role(self)
                return
            if method == "POST" and path == "/org/remove":
                app.handle_post_org_remove(self)
                return
            if method == "POST" and path == "/org/leave":
                app.handle_post_org_leave(self)
                return
            if method == "GET" and path == "/agent-keys":
                app.handle_get_agent_keys(self)
                return
            if method == "POST" and path == "/agent-keys":
                app.handle_post_agent_keys(self)
                return
            if method == "POST" and path == "/agent-keys/revoke":
                app.handle_post_agent_keys_revoke(self)
                return
            if method == "GET" and path == "/config":
                app.handle_get_config(self)
                return
            if method == "POST" and path == "/config":
                app.handle_post_config(self)
                return
            if method == "GET" and path == "/rooms":
                app.handle_get_rooms(self)
                return
            if method == "POST" and path == "/rooms":
                app.handle_post_rooms(self)
                return

            # Room-specific routes
            room_match = _ROOM_PATH_RE.match(path)
            if room_match:
                room_id = room_match.group(1)
                if method == "GET":
                    app.handle_get_room_detail(self, room_id)
                return

            events_match = _ROOM_EVENTS_RE.match(path)
            if events_match:
                room_id = events_match.group(1)
                if method == "GET":
                    app.handle_get_room_events(self, room_id)
                return

            audit_match = _ROOM_AUDIT_RE.match(path)
            if audit_match:
                room_id = audit_match.group(1)
                if method == "GET":
                    app.handle_get_room_audit(self, room_id)
                return

            connect_match = _ROOM_CONNECT_RE.match(path)
            if connect_match:
                room_id = connect_match.group(1)
                if method == "GET":
                    app.handle_get_room_connect(self, room_id)
                return

            close_match = _ROOM_CLOSE_RE.match(path)
            if close_match:
                room_id = close_match.group(1)
                if method == "POST":
                    app.handle_post_room_close(self, room_id)
                return

            revoke_match = _ROOM_REVOKE_LINK_RE.match(path)
            if revoke_match:
                room_id = revoke_match.group(1)
                if method == "POST":
                    app.handle_post_room_revoke_link(self, room_id)
                return

            # Static files
            if method == "GET" and app.static_dir:
                self._serve_static(path)
                return

            app._send_json(self, HTTPStatus.NOT_FOUND,
                           {"error": {"code": "not_found",
                                      "message": "Not found"}})

        def _serve_static(self, path: str) -> None:
            if path == "/":
                path = "/index.html"
            # Resolve-then-assert, never blocklist. A blocklist misses
            # backslashes, rooted absolute paths, drive letters, symlinks
            # and percent-encoded separators; containment on the resolved
            # path catches all of them on every host OS.
            try:
                root = Path(app.static_dir).resolve()
                target = (root / unquote(path).lstrip("/\\")).resolve()
            except (OSError, ValueError):
                app._send_json(self, HTTPStatus.FORBIDDEN,
                               {"error": "forbidden"})
                return
            if not target.is_relative_to(root):
                app._send_json(self, HTTPStatus.FORBIDDEN,
                               {"error": "forbidden"})
                return
            if not target.is_file():
                app._send_json(self, HTTPStatus.NOT_FOUND,
                               {"error": "not_found"})
                return
            import mimetypes
            ctype, _ = mimetypes.guess_type(str(target))
            ctype = _STATIC_CONTENT_TYPE.get(path) or (ctype or "application/octet-stream")
            with open(target, "rb") as f:
                data = f.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for name, value in security_headers(html=ctype.startswith("text/html")):
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._handle_request()

        def do_POST(self) -> None:  # noqa: N802
            self._handle_request()

    return _WebHTTPHandler
