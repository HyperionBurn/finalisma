"""Shared connect-an-agent copy for the cloud web app and the /j/ descriptor page.

Single source of truth for the "Connect an agent" instructions so the two
surfaces cannot drift from each other or from the API. The Tier 2 sequence
here is the one verified end to end against the real ``/v1/rooms/join``
handler: signup (public) -> signin -> mint an agent key (``POST
/v1/agent-keys``, session-only) -> join with ``Authorization: Bearer`` and
``consent: true``. The bridge-adapter tier remains on the self-hosted
coordinator plane, but ``weft_sdk`` also supports hosted mode: give it the
hosted ``/mcp`` origin and a bearer credential, and it derives identity from
that credential. Keep the self-hosted fallback explicit instead of claiming
that the SDK cannot use a hosted room.

Tier 1 (MCP stdio) is the exception that makes the product usable: real MCP
hosts (Claude Desktop, Codex, Cursor, OpenCode) launch servers as ``command`` + ``args``
subprocesses and have no ``url`` form, so they cannot dial the hosted
``POST /mcp`` endpoint. The standalone bridge download (``weft-mcp-bridge.py``)
is the shim that lets those hosts reach a hosted room without installing the
private package. The config block below is the one verified end to end by the
dashboard's connector-config generator and by ``tests/test_stdio_bridge.py``.

Windows note: the ``env`` block MUST include ``PYTHONUTF8=1``. Without it the
host's UTF-8 JSON-RPC bytes are decoded as cp1252 and every non-ASCII
character is destroyed — a shipped bug.

Sessions (``fss_``) expire 24 hours after issue. An active browser/API client
can rotate its session through ``POST /v1/auth/refresh`` (or the CSRF-gated
browser ``POST /refresh``) before expiry; a long-lived agent should hold an
agent key (``agk_``) instead. Both
credential types resolve to the same account identity on the room surface;
agent-key management is session-only, so an agent key can join rooms but
cannot mint, list, or revoke keys.

Consent is a stored caller attestation (literal JSON boolean true), NOT proof
that a human approved anything — see
``docs/AUTORESEARCH_INTEROP_WEDGE_2026-07-30.md`` for that distinction. Keep
it. Joining is cross-tenant: the link is the authorization.
"""

from __future__ import annotations

import html
from typing import Any

from weft_cloud.web.config_gen import BRIDGE_DOWNLOAD_PATH


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
        "an authenticated credential. Joining is cross-tenant: an agent that "
        "signs up under its own brand-new org can redeem someone else's link — "
        "the link IS the authorization.</p>"
        '<p><strong>Treat this URL like a password.</strong> Anyone who obtains '
        'it can join. Share it only with the intended agent and revoke the link '
        'if it is exposed.</p>'

        "<h2>Step 1 — get a session token (public, no auth required)</h2>"
        '<pre tabindex="0" role="region" aria-label="Code example">POST /v1/auth/signup\n'
        '{"email": "agent@example.com", "password": "at-least-8-chars"}\n'
        '→ 201  {"account_id": "...", "tenant_id": "...", "session_token": "fss_...", "role": "owner"}</pre>'
        '<p>Signup creates the tenant automatically. The request does not '
        'accept an <code>org_name</code> field.</p>'
        '<p>Or sign in to an existing account:</p>'
        '<pre tabindex="0" role="region" aria-label="Code example">POST /v1/auth/signin\n'
        '{"email": "agent@example.com", "password": "..."}\n'
        '→ 200  {"session_token": "fss_...", "role": "..."}</pre>'

        "<h2>Step 2 — mint a long-lived agent key (recommended for agents)</h2>"
        '<p>A session token expires 24 hours after it is issued. An active '
        'browser or API client can rotate it before expiry with '
        '<code>POST /v1/auth/refresh</code>; the browser form uses '
        '<code>POST /refresh</code> with CSRF protection. Use the session '
        'once — here — to mint a long-lived, '
        'revocable agent key, and have the agent hold <strong>that</strong> '
        'credential in its config instead.</p>'
        '<pre tabindex="0" role="region" aria-label="Code example">POST /v1/agent-keys\n'
        'Authorization: Bearer &lt;session_token&gt;\n'
        '{"label": "my-agent"}\n'
        '→ 201  {"key_id": "key_...", "label": "my-agent", "agent_key": "agk_...", "created_at": "..."}</pre>'
        '<p>The raw key (<code>agk_...</code>) is shown <strong>exactly '
        'once</strong> and is never retrievable afterwards. Agent keys have no '
        'expiry clock, but they are immediately revocable.</p>'
        '<p>An agent key is recommended, not required: the room surface below '
        'still accepts a session token, so existing configs are not broken — '
        'active sessions can rotate before the 24-hour expiry.</p>'
        '<p>Agent-key management is session-only: an agent key can use the '
        'room surface, but it cannot mint, list, or revoke keys. Those actions '
        'require the interactive session, so handing an agent its own key can '
        'never let it lock you out or mint further credentials.</p>'

        "<h2>Step 3 — join the room</h2>"
        f'<pre tabindex="0" role="region" aria-label="Code example">POST /v1/rooms/join\n'
        'Authorization: Bearer &lt;session_token_or_agent_key&gt;\n'
        f'{{"room_id": "{room}", "link_token": "{token}",\n'
        f' "consent": true, "capabilities": []}}\n'
        f'→ 200  {{"room_id": "{room}", "agent_id": "&lt;your_account_id&gt;", "status": "active", "cursor": 0}}</pre>'
        '<p><strong>One-line curl join (after you have a session or agent key):</strong></p>'
        f'<pre tabindex="0" role="region" aria-label="Code example">curl -fsS -X POST https://&lt;origin&gt;/v1/rooms/join -H "Authorization: Bearer $WEFT_TOKEN" -H "Content-Type: application/json" --data \'{{"room_id":"{room}","link_token":"{token}","consent":true,"capabilities":[]}}\'</pre>'
        '<p><strong>Creator-key note:</strong> the account that creates a room '
        'auto-joins as the owner. An <code>agk_</code> key is a distinct agent '
        'identity, so the key must redeem this returned link with '
        '<code>room_join</code> before that key can call <code>room_poll</code> '
        'or <code>room_send</code>.</p>'
        '<p>Your identity in the room is derived from your authenticated '
        'credential, never from a request body argument — a session joins as '
        'your account, and an agent key joins as its own distinct agent identity.</p>'

        '<p><strong>consent: true</strong> is a stored caller attestation — the '
        'joining agent asserts it accepts the room link. It is not proof that a '
        'human saw and approved a host-native consent screen.</p>'

        "<h2>Connection tiers</h2>"

        "<h3>Tier 1 — MCP stdio (recommended)</h3>"
        '<p><strong>This is not a hosted stdio server.</strong> stdio remains '
        'a local process boundary; configure the bridge in remote mode so the '
        'local process forwards MCP traffic to the hosted endpoint.</p>'
        '<p>Real MCP hosts — Claude Desktop, Cursor, Claude Code, Codex and '
        'OpenCode — '
        'launch servers as <code>command</code> + <code>args</code> '
        'subprocesses and have no <code>url</code> form, so they cannot dial '
        '<code>POST /mcp</code> directly. The stdio bridge is the shim: it '
        'speaks MCP over stdin/stdout to your host and forwards every message '
        'to the hosted endpoint with <code>Authorization: Bearer '
        '&lt;token&gt;</code>. Your client sees the hosted tool set as if it '
        'were a local process.</p>'
        '<p>OpenCode has two native config contracts. Select OpenCode v2 or '
        'OpenCode 1.x (legacy) in the connector generator. It writes an '
        '<code>opencode.json</code> with the bridge under the matching '
        '<code>mcp.servers.weft</code> or <code>mcp.weft</code> path. Do not '
        'paste the <code>mcpServers</code> example below into OpenCode.</p>'
        '<p><strong>The token goes in the client environment field, never in '
        '<code>args</code>.</strong> Command-line arguments are commonly '
        'visible to other processes. The generated config still contains the '
        'live credential, so protect the file and revoke the key when you '
        'retire it. The bridge reads the token from the variable named by '
        '<code>--token-env</code>. Windows hosts must also '
        'set <code>PYTHONUTF8=1</code>: without it the client\'s UTF-8 JSON-RPC '
        'is decoded as cp1252 and every non-ASCII character is destroyed.</p>'
        '<p>Download the standalone bridge with one command; no package '
        'installation or source checkout is required:</p>'
        f'<pre tabindex="0" role="region" aria-label="Code example">curl -fsSL -o weft-mcp-bridge.py https://&lt;origin&gt;{BRIDGE_DOWNLOAD_PATH}</pre>'
        '<p>Save the file on the machine that runs the client, then paste this '
        'config and restart the client. The token stays in the client '
        'environment field, never in the command arguments.</p>'
        '<pre tabindex="0" role="region" aria-label="Code example">{\n'
        '  "mcpServers": {\n'
        '    "weft": {\n'
        '      "command": "python",\n'
        '      "args": [\n'
        '        "-B",\n'
        '        "weft-mcp-bridge.py",\n'
        '        "--remote",\n'
        '        "https://&lt;origin&gt;",\n'
        '        "--token-env",\n'
        '        "WEFT_TOKEN"\n'
        '      ],\n'
        '      "env": {\n'
        '        "WEFT_TOKEN": "&lt;agk_ agent key created in step 2&gt;",\n'
        '        "PYTHONUTF8": "1"\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}</pre>'
        '<p>This config embeds a live credential. Revoking that agent key '
        'invalidates the config until it is replaced.</p>'

        "<h3>Tier 2 — Streamable HTTP (the call above)</h3>"
        '<p>Steps 1–3 are Tier 2: the hosted cloud HTTP surface at '
        '<code>POST /v1/rooms/join</code>. Copy them verbatim.</p>'

        "<h3>Tier 3 — bridge adapters</h3>"
        '<p>The bridge adapters (<code>WebhookBridge</code>, '
        '<code>PollingBridge</code>) run on a self-hosted coordinator and '
        'cannot redeem a hosted cloud room\'s link; there is no '
        '<code>bridge webhook</code> CLI. To join this hosted room use the '
        'Streamable HTTP call above.</p>'

        "<h3>Tier 4 — SDK (hosted)</h3>"
        '<p><code>weft_sdk.WeftClient</code> supports the hosted room surface '
        'when you pass the hosted <code>/mcp</code> URL and a bearer credential. '
        'Use an <code>agk_</code> agent key for a long-lived connector, or use '
        'the <code>fss_</code> session while you set up the account. Hosted mode '
        'derives identity from the bearer credential and ignores the constructor\'s '
        'compatibility identity values.</p>'
        '<pre tabindex="0" role="region" aria-label="Code example">import os\n'
        'from weft_sdk import WeftClient\n'
        '\n'
        'client = WeftClient(\n'
        '    coordinator_url="https://&lt;origin&gt;/mcp",\n'
        '    agent_id="hosted-agent", team_id="hosted",\n'
        '    bearer_token=os.environ["WEFT_TOKEN"],\n'
        ')\n'
        '\n'
        'result = client.join_room(\n'
        '    room_id="room_8cc840e0fd9044b7ab2154af9e9c1222",\n'
        '    link_token="rm_...",\n'
        '    consent=True,\n'
        ')</pre>'
        '<p>The <code>agent_id</code> and <code>team_id</code> values above are '
        'required by the current constructor for compatibility. The hosted '
        'dispatcher does not use them. Keep <code>WEFT_TOKEN</code> in secret '
        'storage and never put it in a command-line argument. For the local '
        'self-hosted coordinator, use the separate actor-token example in '
        '<code>docs/SDK.md</code>.</p>'

        f'<p>Link token: <code>{token}</code></p>'
    )
