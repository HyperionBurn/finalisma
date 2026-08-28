# Weft cross-vendor host transcript — 2026-08-28 (Codex CLI)

## Verdict

**NOT VERIFIED** — Codex CLI `0.149.1` did not expose the Weft MCP tools during
the non-interactive host run. The host returned a structured
`weft_tools_unavailable` result and did not call `room_join`, `room_info`,
`room_poll`, `room_ack`, `room_send`, or `room_leave`. The browser owner did not
render the Codex marker.

This is a negative host result. No adapter, bridge, or server special case was
added to make Codex appear connected.

## Setup that reached the host

- Host: `codex-cli 0.149.1` (OpenAI), a non-Anthropic host product.
- Production origin: `https://weft.switzerlandnorth.cloudapp.azure.com`.
- Browser: cold headless Chrome CDP profile; signup, room creation, key minting,
  and owner messaging used the production UI.
- Room: disposable cap-2 room, short TTL selected. The owner queued the marker
  before the attempted Codex join.
- Codex config: the live `/app/connect` page's **Codex** tab rendered 285-byte
  TOML. It contained `[mcp_servers.weft]`, command `python`, six bridge args,
  live `--remote` origin, `WEFT_TOKEN` under the generated env table, and
  `PYTHONUTF8 = "1"`. The one required local path placeholder substitution used
  the downloaded bridge path. No other MCP server was added.
- Bridge: downloaded from `/downloads/weft-mcp-bridge.py`, HTTP `200`, `18,302`
  bytes, SHA-256
  `468620983c9755c3f277288fad8ef77bc94c782178066ab733eb8ee8042cf9e8`.

The generated config and a temporary copy of the existing Codex auth file were
held under one temporary `CODEX_HOME`. They were removed after the run. No
credential, room link, or temporary path was printed in this transcript.

## Exact host calls and failures

The first invocation used flags shown by `codex exec --help` but rejected them
in the installed CLI parser:

```text
codex exec --json --ephemeral --skip-git-repo-check -a never -s read-only -C <temp> -
→ exit 2
error: unexpected argument '-a' found
tip: to pass '-a' as a value, use '-- -a'
```

The probe then used the accepted global overrides. The generated Codex TOML
remained unchanged:

```text
codex -c approval_policy="never" -c sandbox_mode="read-only" exec --json --ephemeral --skip-git-repo-check
→ exit 0
```

Claude-style MCP names were intentionally not passed as flags or rewritten.
The prompt sent through stdin requested this exact sequence:

```text
room_join(room_id=<room-id>, link_token=<redacted>, consent=true)
room_info(room_id=<room-id>)
room_poll(room_id=<room-id>, after_seq=0, limit=100)
room_ack(room_id=<room-id>, seq=<owner-marker-seq>)
room_send(room_id=<room-id>, target_spec="*", payload={"text":"mpai111-codex-marker"})
room_leave(room_id=<room-id>)
```

Codex's final structured response was:

```json
{
  "join": {"ok": false, "status": "weft_tools_unavailable"},
  "info": {"ok": false, "status": "not_called"},
  "poll": {"ok": false, "status": "not_called", "count": 0},
  "ack": {"ok": false, "status": "not_called"},
  "send": {"ok": false, "status": "not_called"},
  "leave": {"ok": false, "status": "not_called"}
}
```

Measured process evidence: exit `0`, stdout `891` bytes, stderr `493` bytes.
The stderr text was:

```text
WARNING: proceeding, even though we could not create PATH aliases: Refusing to create helper binaries under temporary dir "<temp>" (codex_home: "<temp>")
Reading prompt from stdin...
ERROR codex_skills_extension::loader::host: skills scan reached its traversal limit (root: file:///C:/Users/Wasif/.agents/skills)
```

The host therefore did not reach Weft's live room surface. The browser owner
rendered no `mpai111-codex-marker`. Chrome exited cleanly, and the owner closed
the disposable room after the failed host call.

## Finding and next investigation

The generated Codex server entry was accepted as configuration input, but this
Codex CLI `exec --json` run returned `weft_tools_unavailable` before any room
call. This transcript does not distinguish whether the limitation is Codex
MCP exposure in this execution mode, the local Codex runtime configuration, or
the host's context setup. A future pass must answer that question before the
cross-vendor claim can include Codex.

No tests were added. Test delta is `0`. No build or final gate ran.

