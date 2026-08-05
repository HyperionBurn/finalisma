# Finalisma SDK

A stdlib-only Python client for the [Finalisma A2A protocol](https://github.com/Finalisma/finalisma-mcp).
Talks JSON-RPC over HTTP to a `finalisma-mcp` coordinator using only `http.client`.
No third-party dependencies. Python 3.11+.

## Install

The SDK lives inside the Finalisma monorepo at `src/finalisma_sdk/`.  It ships
with the coordinator — no separate `pip install` needed.  Add the repo's `src/`
to `PYTHONPATH` or install the workspace with `pip install -e .`.

## Quickstart: pair two agents in ~20 lines

```python
from finalisma_sdk import FinalismaClient

COORDINATOR = "http://127.0.0.1:8787/mcp"

# Agent A registers and creates a pairing link
a = FinalismaClient(COORDINATOR, "agent-a", "demo")
reg_a = a.register(name="Planner", role="architect", capabilities=["planning"])
a._actor_token = reg_a["actor_token"]          # persist securely
pairing = a.create_pairing_link(capabilities=["read", "comment"])
print("Share this link with Agent B:", pairing.join_url)

# Agent B joins the pairing (extracts token from URL fragment automatically)
b = FinalismaClient(COORDINATOR, "agent-b", "demo")
join = b.join_pairing(pairing.join_url, consent=True)
b._actor_token = join.actor_token              # persist securely

# A creates a task, B claims and works it
task_id = a.create_task(scope=["src/api.py"], description="Implement /health", title="Implement /health")
task = b.claim(task_id)
b.update_progress(task_id, pct=50, note="half done", fencing_token=task.fencing_token)
b.submit_evidence(task_id, ["src/api.py"],
                  checks=[{"name": "tests", "status": "passed"}],
                  fencing_token=task.fencing_token)
b.complete(task_id, fencing_token=task.fencing_token, summary="Shipped")
```

## Feature table

| Capability | SDK method | Notes |
|---|---|---|
| Connectivity | `connect()` | Returns protocol info; health check. |
| Identity | `register()`, `heartbeat()`, `rotate_credential()` | Token auto-stored; env `FINALISMA_ACTOR_TOKEN` supported. |
| Pairing | `create_pairing_link()`, `join_pairing(link)` | Fragment-token handling is automatic. |
| Tasks | `create_task()`, `claim()`, `update_progress()`, `submit_evidence()`, `complete()` | Typed `TaskResult` with `fencing_token`. |
| Messaging | `ask()`, `send_envelope()` | Envelopes are arbitrary dicts. |
| Sessions | `session_send()`, `session_poll()`, `session_wait()`, `session_ack()` | Ordered, idempotent, replayable events. |
| Errors | `FinalismaError`, `AuthError`, `EvidenceError`, `NotFoundError`, `ConflictError`, `TimeoutError` | Mapped from server error codes. |
| Retry | stdlib exponential backoff | Idempotent methods retry on 408/429/5xx with idempotency keys. |

## SDK API surface

```
FinalismaClient(coordinator_url, agent_id, team_id,
                actor_token=None,    # or FINALISMA_ACTOR_TOKEN env var
                bearer_token=None,   # transport-level HTTP bearer
                timeout=30.0)
  .connect() -> dict
  .register(name, role, model, capabilities, metadata) -> dict
  .heartbeat(task_ids, fencing_tokens) -> dict
  .rotate_credential(current_token) -> CredentialRotation
  .create_pairing_link(capabilities, ttl_seconds) -> PairingResult
  .join_pairing(link, consent, agent_id) -> JoinResult
  .create_task(scope, description, priority, capabilities, title, idempotency_key) -> task_id: str
  .claim(task_id, lease_seconds) -> TaskResult
  .update_progress(task_id, pct, note, fencing_token) -> TaskResult
  .submit_evidence(task_id, artifact_paths, checks, fencing_token) -> dict
  .complete(task_id, fencing_token, summary) -> TaskResult
  .ask(recipient, text, kind) -> dict
  .send_envelope(envelope_dict) -> dict
  .session_send(session_token, kind, payload, idempotency_key, trace_id, agent_id) -> SessionEvent
  .session_poll(session_token, after_seq, limit, agent_id) -> list[SessionEvent]
  .session_wait(session_token, after_seq, timeout_seconds, limit, agent_id) -> list[SessionEvent]
  .session_ack(session_token, seq, agent_id) -> int
  .close()
```

## Token hygiene

- `actor_token` is accepted as a constructor argument **or** via the
  `FINALISMA_ACTOR_TOKEN` environment variable.
- Tokens are **never logged** and **never appear in `__repr__`**.
- After `rotate_credential()`, the client updates its stored token atomically.
- Always persist returned tokens in your host's secret storage; the server
  stores only SHA-256 hashes.

## Retry & idempotency

All mutating calls carry an auto-generated `idempotency_key` and retry up to
4 times with stdlib-only exponential backoff on transient HTTP failures
(408, 429, 500, 502, 503, 504).  Non-idempotent calls (`session_send`,
`join_pairing`, `close_session`) are NOT retried.

## Error mapping

Server error codes are mapped to typed exceptions:

| Server code | SDK exception |
|---|---|
| `actor_auth_required`, `actor_auth_invalid`, `consent_required`, `session_unauthorized`, `session_forbidden` | `AuthError` |
| `quality_gate_required`, `quality_gate_failed` | `EvidenceError` |
| `pairing_not_found`, `task_not_found`, `message_not_found`, `agent_not_registered` | `NotFoundError` |
| `pairing_expired`, `pairing_unavailable`, `pairing_race`, `task_claim_conflict`, `scope_lock_conflict`, `stale_fencing_token`, `lease_expired`, `state_conflict` | `ConflictError` |
| Transport timeout | `TimeoutError` |
| Anything else | `FinalismaError` |

## Security notes

- **Plaintext HTTP**: the SDK talks HTTP.  Put a TLS-terminating reverse proxy
  in front for any non-loopback coordinator.
- **Bearer token**: pass `bearer_token=` to enable HTTP bearer auth at the
  transport layer.  This is separate from the per-agent `actor_token`.
- **Pairing URLs**: the one-time token lives only in the URL `#fragment`.
  `join_pairing()` extracts it and sends it in the POST body.  Never log the
  full URL.
- **No secrets in code**: use environment variables or a secret manager for
  tokens; never hard-code them.
