"""Connector config generator — emit complete, working MCP client configs.

The hosted Weft service is only reachable as a Streamable-HTTP ``POST /mcp``
endpoint with a Bearer token. Real MCP hosts — Claude Desktop, Cursor, Claude
Code, Codex — launch servers as ``command`` + ``args`` stdio subprocesses and
have NO ``url`` form, so this generator emits the installed-module bridge
invocation (``python -B -m weft_mcp --remote <origin> --token-env WEFT_TOKEN``)
wired into each host's native config shape:

- Claude Desktop -> JSON ``mcpServers`` (``claude_desktop_config.json``)
- Cursor        -> JSON ``mcpServers`` (``.cursor/mcp.json``)
- Codex         -> TOML ``[mcp_servers.weft]`` (``~/.codex/config.toml``)

Invariants (see docs/STDIO_BRIDGE.md):
- The token lives in an ``env`` block, NEVER in ``args`` — argv is visible to
  every process on the machine; the environment is not.
- The ``env`` block always includes ``PYTHONUTF8=1``. Without it a Windows host
  decodes the client's UTF-8 JSON-RPC as cp1252 and destroys every non-ASCII
  character. This was a real shipped bug.
- The emitted config embeds a live credential: revoking the agent key
  invalidates the config. Callers must say so.
- Stdlib only — the config text is built here, never by a template engine.

The generated Claude Desktop / Cursor config is verified end to end by
``tests/test_webapp_dashboard.py``: the bridge launched with that exact config
returns the hosted room tools from ``tools/list``.
"""

from __future__ import annotations

import json

#: client_id -> (label, file the config belongs in, output language)
CLIENTS: dict[str, dict[str, str]] = {
    "claude-desktop": {
        "label": "Claude Desktop",
        "file": "claude_desktop_config.json",
        "language": "json",
    },
    "cursor": {
        "label": "Cursor",
        "file": ".cursor/mcp.json",
        "language": "json",
    },
    "codex": {
        "label": "Codex",
        "file": "~/.codex/config.toml",
        "language": "toml",
    },
}

TOKEN_ENV_VAR = "WEFT_TOKEN"


def _toml_str(value: str) -> str:
    """Quote a string for TOML (basic strings), escaping the Windows path."""
    return '"' + (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    ) + '"'


def build_config(client_id: str, agent_key: str, origin: str) -> dict:
    """Return the full generated config for ``client_id``.

    ``agent_key`` is the freshly minted ``agk_`` credential. ``origin`` is the
    hosted service base URL (``--remote``). The customer must install the
    ``weft_mcp`` package in the Python environment used by the MCP host.
    Returns metadata plus the ready-to-paste config text.
    """
    if client_id not in CLIENTS:
        raise ValueError(f"unknown client: {client_id}")
    meta = CLIENTS[client_id]
    args = ["-B", "-m", "weft_mcp", "--remote", origin, "--token-env", TOKEN_ENV_VAR]
    if meta["language"] == "toml":
        config_text = (
            "[mcp_servers.weft]\n"
            "command = \"python\"\n"
            f"args = [{', '.join(_toml_str(a) for a in args)}]\n"
            f"env = {{ {TOKEN_ENV_VAR} = {_toml_str(agent_key)}, "
            f"PYTHONUTF8 = \"1\" }}\n"
        )
    else:
        payload = {
            "mcpServers": {
                "weft": {
                    "command": "python",
                    "args": args,
                    "env": {TOKEN_ENV_VAR: agent_key, "PYTHONUTF8": "1"},
                }
            }
        }
        config_text = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    return {
        "client": client_id,
        "label": meta["label"],
        "file": meta["file"],
        "language": meta["language"],
        "config_text": config_text,
        "origin": origin,
    }
