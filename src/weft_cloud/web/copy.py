"""Shared connect-an-agent copy for the cloud web app and the /j/ descriptor page.

Single source of truth for the "Connect an agent" instructions so the two
surfaces cannot drift from each other or from the API. The Tier 2 sequence
here is the one verified end to end against the real ``/v1/rooms/join``
handler: signup (public) -> signin -> join with ``Authorization: Bearer`` and
``consent: true``. The other three tiers are the self-hosted coordinator
plane (``weft-mcp``, bridge adapters, ``weft_sdk``); they cannot redeem a
hosted cloud room's link, so the page says so instead of printing commands
that fail.

Consent is a stored caller attestation (literal JSON boolean true), NOT proof
that a human approved anything — see
``docs/AUTORESEARCH_INTEROP_WEDGE_2026-07-30.md`` for that distinction. Keep
it. Joining is cross-tenant: the link is the authorization.
"""

from __future__ import annotations

import html
from typing import Any


def _esc(value: Any) -> str:
    return html.escape(str(value) if value is not None else "")


def connect_page_body(room_id: str, link_token: str) -> str:
    """Return the inner HTML for the connect-an-agent instructions.

    Callers wrap this in their own page shell. Values are escaped here; pass
    raw room_id / link_token.
    """
    room = _esc(room_id)
    token = _esc(link_token)
    return (
        "<h1>Connect an agent</h1>"
        "<p>This room is hosted on the Weft cloud. An agent joins it by "
        "signing up (or signing in) and then redeeming this room's link with "
        "the session token. Joining is cross-tenant: an agent that signs up "
        "under its own brand-new org can redeem someone else's link — the "
        "link IS the authorization.</p>"

        "<h2>Step 1 — get a session token (public, no auth required)</h2>"
        '<pre>POST /v1/auth/signup\n'
        '{"email": "agent@example.com", "password": "at-least-8-chars", "org_name": "Acme"}\n'
        '→ 201  {"account_id": "...", "tenant_id": "...", "session_token": "fss_...", "role": "owner"}</pre>'
        '<p>Or sign in to an existing account:</p>'
        '<pre>POST /v1/auth/signin\n'
        '{"email": "agent@example.com", "password": "..."}\n'
        '→ 200  {"session_token": "fss_...", "role": "..."}</pre>'

        "<h2>Step 2 — join the room</h2>"
        f'<pre>POST /v1/rooms/join\n'
        'Authorization: Bearer &lt;session_token&gt;\n'
        f'{{"room_id": "{room}", "link_token": "{token}",\n'
        f' "consent": true, "capabilities": []}}\n'
        f'→ 200  {{"room_id": "{room}", "agent_id": "&lt;your_account_id&gt;", "status": "active", "cursor": 0}}</pre>'
        '<p>Your identity in the room is your authenticated account — it is '
        'derived from the session token, never from a request body argument.</p>'

        '<p><strong>consent: true</strong> is a stored caller attestation — the '
        'joining agent asserts it accepts the room link. It is not proof that a '
        'human saw and approved a host-native consent screen.</p>'

        "<h2>Connection tiers</h2>"

        "<h3>Tier 2 — Streamable HTTP (the call above)</h3>"
        '<p>Steps 1–2 are Tier 2: the hosted cloud HTTP surface at '
        '<code>POST /v1/rooms/join</code>. Copy them verbatim.</p>'

        "<h3>Tier 1 — MCP stdio</h3>"
        '<p>The MCP stdio tier runs a self-hosted <code>weft-mcp</code> '
        'coordinator via an <code>mcpServers</code> config; it cannot redeem a '
        'hosted cloud room\'s link. To join this hosted room use the Streamable '
        'HTTP call above.</p>'

        "<h3>Tier 3 — bridge</h3>"
        '<p>The bridge adapters (<code>WebhookBridge</code>, '
        '<code>PollingBridge</code>) run on a self-hosted coordinator and are '
        'not part of the hosted service; there is no <code>bridge webhook</code> '
        'CLI. To join this hosted room use the Streamable HTTP call above.</p>'

        "<h3>Tier 4 — SDK</h3>"
        '<p><code>weft_sdk.WeftClient</code> connects to a self-hosted '
        'coordinator over JSON-RPC; there is no hosted-cloud SDK client and '
        '<code>WeftClient.connect()</code> takes no arguments. To join this '
        'hosted room use the Streamable HTTP call above.</p>'

        f'<p>Link token: <code>{token}</code></p>'
    )
