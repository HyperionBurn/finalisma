# CORRECTION to DOGFOOD FINDING #3 — do NOT make register_agent return an existing credential

**I was wrong, and implementing finding #3 as written would be a security regression.**

Verified in `src/finalisma_mcp/core.py`:

```sql
token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64)
```

Actor tokens are stored as **SHA-256 hashes only**. The raw token is never persisted, so returning
an existing credential is not merely unimplemented — it is cryptographically impossible without
storing raw secrets. `site/docs/quickstart.html` already documents this as intentional:

> "Persist the returned `actor_token` once in the host's secret storage. Finalisma stores only its
> SHA-256 hash and will never return the raw token again."

That is a good design. Do not weaken it.

**What to do instead** — the real problem is recovery and discoverability, not retrieval:

1. `rotate_agent_credential(team_id, agent_id, current_token=None)` already exists and
   `current_token` is OPTIONAL — that is the intended recovery path for an agent that lost its
   token. Confirm the semantics, then make the error you get when calling a mutation without a
   valid token NAME that tool: something like
   *"Actor token invalid or missing. Call `register_agent` for a new agent, or
   `rotate_agent_credential` to reissue a credential for an existing one."*
2. Say plainly in `register_agent`'s description that the returned `actor_token` is shown **once
   and never again**, and must be persisted by the caller.
3. If `rotate_agent_credential` without `current_token` is deliberately restricted (e.g. owner-only),
   document that restriction rather than loosening it.

**Finding #3 in `docs/DOGFOOD_FINDINGS_2026-08-07.md` should be rewritten** to reflect this:
the gap is that nothing tells you the token is one-time and nothing points at the rotation path —
NOT that idempotent retrieval is missing.
