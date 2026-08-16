# Weft stdio↔hosted MCP bridge

**Status:** implemented (`src/weft_mcp/stdio_bridge.py`), wired into
`weft-mcp --remote`, 17 integration tests (`tests/test_stdio_bridge.py`) driving
the real code path a stdio MCP host uses. Authoritative usage lives here.

## The problem it solves

The hosted Weft service (`weft_cloud`, e.g. `https://weft.switzerlandnorth.
cloudapp.azure.com`) exposes its rooms as a **Streamable-HTTP** MCP endpoint:
`POST /mcp`, authenticated with a Bearer session token or agent key. The local
hosted-service test suite verifies the contract — 401 unauthenticated, 200
authenticated, and 11 room tools. A fresh public-deployment probe is still
required before treating those results as current production behavior.

But the MCP hosts people actually use speak **stdio**. Claude Desktop, Cursor,
Claude Code, and Codex all launch an MCP server as a subprocess: a `command`
plus `args` in a config file, talking JSON-RPC over stdin/stdout. None of them
has a `url` form. So today those clients can only reach the **local**
coordinator, which has its own separate database and therefore its own separate
rooms. That turns Weft into a self-hosted tool — the opposite of what the
hosted service is for.

The bridge is the shim between the two worlds: it speaks MCP over stdin/stdout
to the client and forwards every JSON-RPC message to the hosted `/mcp` endpoint
with `Authorization: Bearer <token>`, returning the replies over stdout. The
client sees the hosted tool set as if it were a local process.

## Invocation

```bash
weft-mcp --remote https://weft.switzerlandnorth.cloudapp.azure.com --token-env WEFT_TOKEN
```

- `--remote <origin>` — base URL of the hosted service; the bridge POSTs to
  `<origin>/mcp`. No `--state`/`--workspace`/`--team-id` are used in this mode:
  there is no local coordinator and no local database. Tenancy comes from the
  token, exactly as on the hosted REST surface.
- `--token-env <NAME>` — the **name of an environment variable** that holds the
  bearer token. This reuses the existing `--token-env` flag from the local HTTP
  mode; in remote mode it is required. **The token is read from the
  environment only — never from a command-line argument**, because argv is
  visible to every process on the machine.

The recommended token is a long-lived, revocable **agent key** created with a
session from `POST /v1/agent-keys` (`agk_` prefix). Sign up or sign in first to
obtain the interactive session (`fss_` prefix), then use that session for key
creation and later key management. MCP accepts either credential, but the
session has a default 24 h TTL while the agent key has no expiry clock. Do not
put the raw token in a config file; put it in the process environment.

## Client config block (ready to paste)

Claude Desktop / Cursor / Claude Code / Codex all use the same `command` +
`args` shape. Set the token in the environment of the client process (or the
shell that launches it) as `WEFT_TOKEN`.

```json
{
  "mcpServers": {
    "weft": {
      "command": "python",
      "args": [
        "-B",
        "<absolute-path-to-repo>/scripts/weft-mcp.py",
        "--remote",
        "https://weft.switzerlandnorth.cloudapp.azure.com",
        "--token-env",
        "WEFT_TOKEN"
      ],
      "env": {
        "WEFT_TOKEN": "<agk_ agent key created with POST /v1/agent-keys>"
      }
    }
  }
}
```

Some hosts only allow `env` to reference existing environment variables rather
than define new ones; in that case export `WEFT_TOKEN` in the shell before
launching the client and drop the `env` block. For MCP, prefer the agent key
(`agk_`) rather than the interactive session (`fss_`). Agent keys have no
expiry clock but remain revocable; password resets or membership removal
invalidate them, while role changes are re-derived on future requests. Use the
session-only `/v1/agent-keys` routes to create,
list, and revoke keys.

## Behaviour

- **Full JSON-RPC passthrough.** `initialize`, `notifications/initialized`,
  `tools/list`, `tools/call`, `ping` — all forwarded unchanged. The tool set is
  never filtered or rewritten: whatever the hosted endpoint exposes is exactly
what the client sees (the 11 hosted room tools).
- **Both Streamable-HTTP response shapes are unwrapped.** The hosted endpoint
  may reply with plain JSON (`application/json`) or an SSE-framed body
  (`text/event-stream`: `event:` / `data:` lines, including multi-line `data`).
  Both parse; getting this wrong looks like a silent hang to the client.
- **`Mcp-Session-Id` is preserved.** If the server issues one in a response
  header, the bridge keeps it and echoes it on every subsequent request.
- **Notifications produce no reply line** — matching the JSON-RPC/MCP
  convention the local coordinator already follows. A 202 acknowledgement from
  the upstream is consumed without a stdout write.
- **Without `--remote`, nothing changes.** The flag is additive: local
  coordinator mode (stdio or HTTP, local SQLite store) is byte-for-byte
  unchanged and covered by the existing suite.

## Failure mode discipline

stdio hosts swallow stderr, so every misconfiguration surfaces as a JSON-RPC
error the client will *display*, never a silent exit:

| Condition | What the client sees |
| --- | --- |
| `WEFT_TOKEN` unset/empty | JSON-RPC error: `no Weft bearer token found: environment variable WEFT_TOKEN is not set or empty`; process exits non-zero |
| Upstream returns 401 | JSON-RPC error naming the cause (deploy, password reset, expiry, or key revocation) and the recovery: re-authenticate at `POST /v1/auth/signin` for a session or create a replacement agent key at `POST /v1/agent-keys`, export it in the `--token-env` variable, and restart the client |
| Connection refused / DNS failure | JSON-RPC error naming the origin it tried, e.g. `could not reach the Weft hosted MCP endpoint at http://127.0.0.1:1234 (ConnectionRefusedError: ...)` |
| Upstream non-200 | JSON-RPC error: `the Weft hosted endpoint at <origin> returned HTTP <status>` |
| Unparseable body | JSON-RPC error: `the Weft hosted endpoint returned an unparseable response` |

## Testing

`tests/test_stdio_bridge.py` drives the real bridge subprocess
(`scripts/weft-mcp.py --remote ... --token-env ...`) against a local instance
of the hosted service on a temp SQLite-WAL DB:

- full stdio session: `initialize` → `tools/list` → `tools/call(room_create)`;
- SSE-framed and plain-JSON bodies both parse (including multi-line `data`);
- `Mcp-Session-Id` is preserved across requests;
- missing token → JSON-RPC error naming the env var + non-zero exit;
- upstream 401 → renderable JSON-RPC error, no hang;
- connection refused → error names the origin;
- **two separate bridge processes with distinct identities join the
  same room via the same link and exchange a message** (two accounts — or,
  with the one-key-one-identity model, two agent keys from a single
  account) — the product's entire
  promise exercised through the exact code path a real client uses;
- parser test proving `--remote` is additive and local defaults are untouched.

```powershell
python -B -m unittest tests.test_stdio_bridge -v
```

For a concrete process-level exchange, run the standalone evidence driver:

```powershell
python -B .\scripts\prove-two-bridge.py
```

It starts one disposable local hosted service, launches two independent bridge
processes with separate bearer credentials, and prints
`RESULT: two-bridge shared-room message delivered: True` on success. Its
SQLite database and subprocess workspace live under the ignored project-local
`.tmp/` directory and are removed on exit.

## Security notes

- The token is read from an environment variable only; it is never placed in
  argv, never logged, and never written to the config file.
- Token prefixes (`rm_`, `fst_actor_`, `fss_`) and the `weft.a2a` namespace are
  unchanged by this module.
- The bridge never touches the hosted request body or response — it is a
  transparent forwarder, so the hosted endpoint's own auth, tenancy, and
  no-oracle guarantees are inherited unchanged.
