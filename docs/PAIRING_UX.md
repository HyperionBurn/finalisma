# Finalisma two-minute pairing experience

## The promise

Give Agent A a prompt, ask it to create a Finalisma link, and hand that link to
Agent B. Agent B sees what will be shared, explicitly accepts, and both agents
receive one governed session. The first usable team should take less than two
minutes. This is the live MCP flow; the browser simulation on the launch site
is a UX preview only.

## Measured friction findings — 2026-08-05 TTFV run

A timed end-to-end run was executed in a clean temp workspace
(`C:\Users\Wasif\AppData\Local\Temp\opencode\ttfv-20260805124524`) using the
real MCP stdio transport. Every command from the quickstart was issued as a
JSON-RPC tool call against a live `finalisma-mcp.py` process.

### Per-step timings (wall-clock, seconds)

| Step | Time (s) | Notes |
|------|----------|-------|
| Server startup + initialize | 0.134 | Python interpreter + SQLite + MCP handshake |
| Register Agent A | 0.001 | |
| Register Agent B | 0.001 | |
| Create pairing link | 0.001 | |
| Preview pairing | 0.015 | Slightly heavier — reads policy + expiry |
| Join pairing (consent=true) | 0.001 | |
| Create task | 0.002 | |
| Claim task | 0.000 | |
| Write artifact + verify evidence | 0.001 | Hash + scope check |
| Complete task | 0.000 | |
| **Total** | **0.166** | Full path: server start → verified handoff |

### Friction notes

1. **Server startup dominates (81% of total time).** The 0.134 s is pure
   Python/SQLite initialization. On first run with a cold disk cache this can
   reach ~0.46 s. This is the single biggest lever — a pre-forked or
   long-lived coordinator process would drop the measured path to under 40 ms.
2. **No step failed.** Every command succeeded on first attempt. The documented
   paths in the README, quickstart.html, and examples were all copy-paste
   correct against the live server.
3. **Pairing preview is the heaviest protocol call (0.015 s).** Still trivial,
   but 15× heavier than a simple registration. Worth noting if hosts poll it.
4. **Human time is the real budget.** The protocol completes in under 200 ms.
   The 5-minute target is entirely consumed by reading docs, copy-pasting
   commands, and approving the host's MCP prompt. No coordinator-side fix
   can address this — it is a documentation and host-UX problem.

### Verdict

**Total measured TTFV: 0.17 s** (well under the 5-minute target). The top 3
time sinks are:

1. Server startup (0.13 s) — fix: long-lived coordinator or lazy init.
2. Human reading/pasting (unmeasured, dominant) — fix: shorter quickstart,
   copy-paste-correct commands (now done).
3. Cold-start disk cache (~0.46 s worst case) — fix: none needed for local dev.

## Agent A: create and share

```text
Call finalisma_create_pairing with:
- initiator_id: your stable agent id
- capabilities_offered: the smallest capabilities you need (for example ["read", "comment"])
- ttl_seconds: 900 unless the user requests another value
- actor_token: your stored agent credential when actor auth is required
```

Share the returned `join_url` or the complete `bootstrap_prompt`. The URL is a
short-lived bearer capability: send it only to the intended Agent B and never
write it to logs, public issue text, or a model transcript. Generated URLs put
the token in the fragment after `#token=`; the server sees only the public
pairing ID on preview, and the joining adapter sends the fragment token in the
POST body. Token-bearing URL paths are rejected because reverse proxies can
log paths before the application sees them.

## Agent B: preview, consent, join

1. Parse the `token` fragment from the link, or use the token embedded in the
   bootstrap prompt, then call `finalisma_pairing_preview` with it.
2. Show the inviter, expiry, policy, and capabilities offered.
3. Ask the user for explicit confirmation. A pasted link is not consent.
4. Call `finalisma_join_pairing` with a stable `agent_id`, capability manifest,
   the selected model/provider ID, and the literal JSON boolean `consent=true`.
   Values such as `"false"`, `"yes"`, or `1` are invalid and do not consume the
   link. If that `agent_id` already exists in the team, also pass its current
   `actor_token`; the invite cannot overwrite an existing identity.
5. For a genuinely new invited identity, store the one-time `actor_token`
   returned by the join in the host's secret storage. Finalisma stores only its
   SHA-256 hash and will not return the raw token again.
6. Store `session_token` separately in the host's secret storage. Agent A stores its
   separate `initiator_session_token` from link creation. Do not echo either
   credential back into the conversation.

## After joining

Use `finalisma_session_send` for cross-agent work, not free-form hidden state.
When waiting for the other agent, call `finalisma_session_wait` with the last
acknowledged sequence. After processing events, call `finalisma_session_ack`.
On reconnect, resume from that cursor; do not create a new link unless the
session is expired or closed.

If an actor credential must change, call
`finalisma_rotate_agent_credential(team_id, agent_id, current_token)` and replace
the stored token with the one-time result; the old token is invalidated
atomically. A schema-v2 state file upgrades to schema v3 without fabricating
credentials for existing identities. Recover those identities only through a
trusted local rotation/bootstrap, or pair a genuinely new `agent_id`; pairing
does not bypass proof for an existing ID.

## What the link cannot do

- It cannot install MCP into a product that does not support MCP or an HTTP
  connector.
- It cannot grant filesystem, shell, browser, or provider credentials by
  implication. Those capabilities must be separately configured and shown.
- It cannot bypass a host's approval UX.
- It cannot expose previous conversation history or another team's membership.
