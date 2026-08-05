# Finalisma interop validation — 2026-08-05

## Summary

Finalisma was validated against a **real MCP host** (opencode v1.18.13, the host
this repository's development session runs inside) and through an **independent
stdio MCP wire client**. Both succeeded. The host validation exposed one genuine
interop defect, which is now fixed and re-validated.

| Tier | Surface | Result |
| --- | --- | --- |
| Tier 1 (host) | opencode v1.18.13 loads finalisma from its own project config (`type: local`, stdio) | **verified** — full handoff completed by the host |
| Tier 2 (protocol) | independent stdio MCP client (`scripts/interop-validate.py`) | **passed** — full lifecycle + negative case |

## Tier 2 — independent stdio protocol validation

Driver: `scripts/interop-validate.py` (Python stdlib, spawns the coordinator
over stdio, speaks real MCP JSON-RPC, tears down in `finally`).

Command:

```powershell
timeout 180 python -B scripts/interop-validate.py
```

Result: `status: ok`, `time_to_verified_handoff_s: 0.307`.

1. **initialize** → `protocolVersion: 2025-11-25`, `serverInfo: finalisma-mcp/0.1.0`.
2. **tools/list** → 46 tools; all required lifecycle tools present.
3. Register `agent-a` + `agent-b`, each issued a one-time `actor_token`.
4. `finalisma_create_pairing` → `pairing_id`, `join_token`, fragment-based `join_url`.
5. `finalisma_pairing_preview` → `status: issued`, consent required.
6. `finalisma_join_pairing` (consent=true) → `session state: active`, members `[agent-a, agent-b]`.
7. `finalisma_create_task` → `status: pending`, dispatch envelope emitted.
8. `finalisma_claim_task` → `in_progress`, fencing token issued.
9. Write artifact, `finalisma_verify_task` → `passed: true`, `task_status: verified`.
10. `finalisma_complete_task` → `status: done`.
11. **Negative case**: reusing the one-use pairing link → refused with
    `pairing_unavailable: Pairing is consumed`.

## Tier 1 — real host validation (opencode)

Host: **opencode 1.18.13** (JavaScript/TypeScript MCP client). Config file:
`C:\Users\Wasif\AppData\Local\Temp\opencode\interop-host\opencode.json`
(scratch project config; the repository's global config is untouched).

Schema used (opencode MCP local-server shape):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "finalisma": {
      "type": "local",
      "command": ["python", "-B", "<repo>/scripts/finalisma-mcp.py",
        "--transport", "stdio", "--team-id", "demo",
        "--workspace", "<tmp>/interop-host/ws",
        "--state", "<tmp>/interop-host/ws/.finalisma/state.db"],
      "enabled": true,
      "timeout": 10000
    }
  }
}
```

`opencode mcp list` from that directory reported `✓ finalisma connected`.

A headless agent run (`opencode run -m LongCat/LongCat-2.0`) drove the tools
through opencode's own MCP surface and completed the full lifecycle:

| Step | Result |
| --- | --- |
| `finalisma_register_agent` × 2 | completed — both active |
| `finalisma_create_pairing` | completed |
| `finalisma_pairing_preview` | completed — requires consent |
| `finalisma_join_pairing` (consent=true) | completed — session active, both members |
| `finalisma_create_task` | completed |
| `finalisma_claim_task` | completed — fencing token issued |
| write artifact | completed |
| `finalisma_verify_task` | completed — **passed**, task verified |
| `finalisma_complete_task` | completed — **done** |

## Defect found and fixed

**First host run failed at verify/complete.** The fencing token
(`8629348281316227565`) exceeds JavaScript's safe-integer limit
(2^53−1 = 9007199254740991). opencode's JSON transport serialized it as a
JS number, precision was lost, and the server rejected every follow-up call
with `stale_fencing_token`.

Root cause: `src/finalisma_mcp/core.py` generated tokens with
`secrets.randbits(63)` (up to 2^63−1), far outside the JS-safe range. Real MCP
hosts (opencode, Claude Desktop, Cursor) are all JavaScript/TypeScript clients,
so this is a genuine interop bug, not a test artifact.

Fix: `secrets.randbits(51)` (≤ 2^51−1), inside the JS safe-integer range.
Guard: `tests/test_finalisma.py::test_fencing_token_fits_javascript_safe_integer`
(32 claims, asserts every token ≤ 2^53−1). Re-ran the host validation after the
fix: **full handoff completed**.

## Status labels

- opencode — host row moves from `documented-unverified` to **`verified`**
  (fresh end-to-end host run, config file + transcript in this repository).
- All other hosts remain `documented-unverified` — transport support is
  documented but no fresh Finalisma host run exists for them yet.
- Protocol row (stdio wire client) recorded as protocol validation, distinct
  from host validation.

## Files

- `scripts/interop-validate.py` — repeatable stdio protocol driver.
- `tests/test_finalisma.py` — JS-safe fencing token regression test.
- `src/finalisma_mcp/core.py` — fencing token now JS-safe.
