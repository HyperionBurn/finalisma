# Finalisma security review — 2026-08-05

Read-only adversarial review of the Wave-A attack-surface doubling.
Scope: `tenancy.py`, `roster.py`, `outbox.py`, `bridge.py`, `sdk/client.py`.
Baseline: `docs/BASELINE_2026-08-05.md`. Contract: `docs/SECURITY_GATES.md`.

## Result summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 2 |
| Medium | 4 |
| Low | 3 |

## CRITICAL / HIGH summary

1. **HMAC key-is-hash fallback lets anyone with the DB forge valid webhook signatures** — `bridge.py:221-224`. Gate: *pairing/session tokens are opaque and stored hashed* (the same hygiene applied to webhook secrets is violated by the fallback).
2. **SDK HTTP-error path leaks the coordinator response body into an exception** — `sdk/client.py:264`. Gate: *session tokens are never returned by preview endpoints, never logged*.

## Detailed findings

### HIGH-1 — WebhookBridge fallback signing key is the stored SHA-256 hash

**Gate violated:** SECURITY_GATES.md §"Pairing/session tokens are opaque and stored hashed; token-bearing URL paths are rejected" — the same "hash only at rest" hygiene is claimed for webhook secrets, but the code deliberately uses the hash as a live HMAC key.

**Evidence:** `bridge.py:201-258` `WebhookBridge.deliver`:
```
221:        if signing_secret is not None:
222:            signing_key = signing_secret.encode("utf-8")
223:        else:
224:            signing_key = secret_hash.encode("utf-8")
```
And `verify_signature` (`bridge.py:260-280`) recomputes the expected MAC with the caller-supplied `secret` directly:
```
276:        signed_payload = f"{timestamp}.{json.dumps(body, sort_keys=True, separators=(',', ':'))}"
277:        expected = "sha256=" + hmac_mod.new(
278:            secret.encode("utf-8"), ...
```

**Attack scenario.** The docstring at line 202-208 admits the fallback is for "test/self-test contexts", but the code path is keyed only on `signing_secret is None`, not on a test flag. In production, any caller that omits `signing_secret` silently signs with `SHA-256(secret)`. Because `verify_signature` accepts the *raw* secret, a recipient can verify a signature produced with the hash-as-key — so the scheme works, but the security bound collapses:
- Anyone who can read the `bridge_webhooks.secret_hash` column (SQL injection, a backup leak, a shared multi-tenant DB, a DBA) can forge a valid `X-Finalisma-Signature` for *any* event to that webhook, without ever knowing the real secret. The stored hash *is* the signing key.
- HMAC's security proof assumes the key is high-entropy and secret. Using a hash of the secret as the key reduces effective security to "whoever can read the hash column", which is a strictly weaker bound than the raw secret.

**Canonicalisation.** `_hmac_sign` and `verify_signature` use identical payload construction (`f"{timestamp}.{json.dumps(body, sort_keys=True, separators=(',', ':'))}"`), so there is no canonicalisation mismatch between sign and verify. Timestamp replay window (`max_age_seconds=300`) is enforced at `bridge.py:273-275`. These parts are correct.

**Recommended fix.** Remove the hash-as-key fallback. `WebhookBridge.deliver` should require the caller to supply the raw `signing_secret` and raise if it is absent. The bridge already stores only the hash at rest (`bridge.py:180`); the caller is responsible for providing the live secret from its own secret store. If a self-test path is needed, pass the real secret explicitly.

---

### HIGH-2 — SDK HTTP-error path leaks coordinator response body into an exception

**Gate violated:** SECURITY_GATES.md §"Session tokens are never returned by preview endpoints, never logged" — response bodies on actor-auth failures can contain echoed tokens or internal details, and the SDK embeds them in a raised exception that any caller's log handler may capture.

**Evidence:** `sdk/client.py:263-264`:
```
263:                if status != 200:
264:                    raise FinalismaError("http_error", f"HTTP {status} from coordinator", {"status": status, "body": body.decode("utf-8", errors="ignore")[:500]})
```

**Attack scenario.** When the server returns a 4xx/5xx (e.g. `actor_auth_invalid` after a rotation, or an error envelope that echoes the rejected token), the SDK truncates the body to 500 bytes and attaches it as `details` to the exception. A typical application logs `repr(exc)` or `exc.details`. If the server ever includes the rejected `actor_token` or a session token in its error body (the server currently does not, but the contract is one-way — the SDK cannot assume), it lands in the SDK caller's logs. Even under the current server, the body can contain the caller's own `actor_token` if the server ever echoes the failed credential in a debug field.

**Recommended fix.** Redact the body before attaching: keep `{"status": status}` only, and never propagate raw response bytes into the exception. If a body snippet is needed for debugging, strip anything matching `SECRET_PATTERNS` from `core.py:99-105` and replace token-shaped strings.

---

### MEDIUM-1 — `tenancy.derive_actor_key` is a deterministic, un-keyed hash of public inputs

**Gate violated:** SECURITY_GATES.md §"Shared transactional storage with tenant/owner filtering enforced at the data layer" — the `actor_key` is the data-layer ownership token, but it is trivially forgeable.

**Evidence:** `tenancy.py:208-216`:
```
208: def derive_actor_key(org_id: str, agent_id: str) -> str:
216:     return hashlib.sha256((org_id + agent_id).encode("utf-8")).hexdigest()
```

**Attack scenario.** `org_id` and `agent_id` are not secret (they are primary keys exchanged in the clear). Anyone who knows an `(org_id, agent_id)` pair — which is every member of the org, every URL, every log line — can compute the `actor_key` for any other member and pass `assert_scope` (`tenancy.py:223-233`) as them. The membership check (`is_member`) still gates, so this is not a direct cross-org read, but it weakens the "actor key proof" to "knowing two public identifiers". The stored `actor_key` in `tenancy_claims` is therefore not a capability; it is a checksum. The name over-promises.

**Recommended fix.** Either (a) treat `actor_key` as a non-secret correlation id and rename it to avoid the capability implication, or (b) derive it with a server-wide secret, e.g. `HMAC-SHA256(server_secret, org_id || agent_id)`, so it becomes an unforgeable capability. Option (b) matches the product's "evidence gates / actor credentials" framing.

---

### MEDIUM-2 — `tenancy.assert_scope` does not bind the caller to the supplied `actor_key_hex`

**Gate violated:** SECURITY_GATES.md §"A remote single-workspace deployment can set `--team-id` so a bearer token cannot address another team" — the equivalent gate for tenancy is missing: the caller supplies `org_id`, `agent_id`, *and* `actor_key_hex` freely.

**Evidence:** `tenancy.py:223-233`:
```
223: def assert_scope(db_path: str, org_id: str, agent_id: str, actor_key_hex: str) -> None:
229:     expected = derive_actor_key(org_id, agent_id)
230:     if not _secure_compare(expected, actor_key_hex):
231:         raise ScopeError("actor_key mismatch for org")
232:     if not is_member(db_path, org_id, agent_id):
233:         raise ScopeError("agent is not a member of org")
```

**Attack scenario.** A caller can pass *any* `org_id` and *any* `agent_id` they know, supply the derived key (trivial per MEDIUM-1), and `claim_resource` (`tenancy.py:240-262`) will record the claim under that `(org_id, actor_id)` pair. Combined with MEDIUM-1, this means any org member can write a claim as any other member of the same org. The membership check prevents cross-org writes, but not intra-org impersonation.

**Recommended fix.** The caller's identity must be established by an unforgivable proof *before* `assert_scope` is called (e.g. an OAuth/OIDC-validated `(org_id, agent_id)` pair, per the module's own docstring at lines 8-13), and `assert_scope` should receive the already-authenticated identity rather than a free-form triple. The `actor_key` should then be an additional server-secret-sealed capability (MEDIUM-1 option b).

---

### MEDIUM-3 — `roster.py` and `outbox.py` tables have no `org_id` / tenant column

**Gate violated:** SECURITY_GATES.md §"Shared transactional storage with tenant/owner filtering enforced at the data layer, not only at HTTP handlers" — roster/outbox/bridge tables are owned by `roster_id` / `team_id` but there is no row-level tenant isolation in the schema.

**Evidence:**
- `roster.py:80-112` — `roster_members`, `roster_groups`, `roster_links` are keyed only by `roster_id`. No `org_id`.
- `outbox.py:83-108` — `outbox_entries` has `roster_or_team_id` (a string, not a FK) and `outbox_dlq` has *no* tenant column at all.
- `bridge.py:97-136` — `bridge_outbox`, `bridge_cursors` are keyed by `(team_id, agent_id)`, but `bridge_webhooks` and `bridge_bootstrap_nonces` are not tenant-scoped beyond `team_id` in the row.

**Attack scenario.** The module-level `_store` / `_db_path` singletons (`roster.py:473`, `outbox.py:68`) mean a process can only point at one DB file at a time, which provides accidental isolation in the single-node preview. But the schema itself does not enforce tenant ownership: if a future multi-tenant deployment mounts two orgs in one DB (the SECURITY_GATES.md multi-instance path), `list_members(roster_id)` will return the right rows only if the caller supplies the right `roster_id` — there is no structural barrier to passing another org's `roster_id`. The `outbox_dlq` table is the worst case: it has no tenant column, so a shared-DB operator peeking at the DLQ sees every org's dead-lettered payloads.

**Recommended fix.** Add an `org_id` column to every `roster_*`, `outbox_*`, and `bridge_*` table with a foreign key or at least a check, and filter every query by it. For `outbox_dlq`, add `org_id` immediately — it stores full `payload_json` envelopes.

---

### MEDIUM-4 — `outbox.payload_json` contains full envelopes with no replay-to-wrong-recipient protection

**Gate violated:** SECURITY_GATES.md §"A distributed limiter and transactional outbox/dead-letter queue for event delivery and retry" — retry semantics are safe against double-delivery but not against mis-delivery.

**Evidence:** `outbox.py:186-221` `enqueue` serialises the whole envelope to `payload_json`. `claim_due` (`outbox.py:224-255`) returns rows including `payload_json`. `mark_retry` (`outbox.py:272-330`) re-queues with the same `payload_json`. The `recipient` column is set at enqueue time and never re-checked at delivery.

**Attack scenario.** If a bug or race in the fan-out layer writes the wrong `recipient` into an entry (or an attacker who can write to the outbox table flips `recipient`), the envelope — which may contain task scope, evidence, or session-bearing correlation ids — is delivered to a different recipient on the next retry. The per-recipient idempotency key (`outbox.py:99-100`) prevents *duplicate* delivery, not *mis*-delivery. The DLQ (`outbox.py:288-311`) preserves the mis-addressed payload for replay.

**Recommended fix.** At delivery time, re-derive the intended recipient from the envelope and assert it matches the `recipient` column, or encrypt the envelope to the recipient's key so a flipped recipient cannot read it. At minimum, do not re-enqueue on `retry_dlq` without re-validating the recipient against the current roster.

---

### LOW-1 — `_secure_compare` in `tenancy.py` is not constant-time on content

**Evidence:** `tenancy.py:290-297`:
```
290: def _secure_compare(a: str, b: str) -> bool:
291:     if len(a) != len(b):
292:         return False
293:     result = 0
294:     for x, y in zip(a, b):
295:         result |= ord(x) ^ ord(y)
296:     return result == 0
```

**Issue.** The early-out on length mismatch (line 291-292) leaks string length via timing. For a 64-char hex hash this is a minor leak, but `hmac.compare_digest` (used correctly in `core.py:583` and `bridge.py:89`) is the standard. The local reimplementation is redundant and slightly weaker.

**Recommended fix.** Replace with `hmac.compare_digest(expected, actor_key_hex)`.

---

### LOW-2 — `bridge._authorize_actor` does not check `require_actor_auth` and always demands a token

**Evidence:** `bridge.py:74-90` unconditionally raises `BridgeAuthError` when `actor_token is None`, regardless of the store's `require_actor_auth` setting.

**Issue.** This is stricter than `core.py`'s `_authorize_actor` (which respects `require_actor_auth=False`). A deployment that intentionally runs with `require_actor_auth=False` on stdio will still be rejected by every bridge operation. Not a vulnerability, but a correctness mismatch that will surface as a confusing failure when the bridge is used in `auto` mode.

**Recommended fix.** Mirror `core._authorize_actor`'s optional-auth behaviour, or document that bridge operations always require actor auth.

---

### LOW-3 — SDK `rotate_credential` silently falls back to `self._actor_token` as `current_token`

**Evidence:** `sdk/client.py:362-366`:
```
362:     def rotate_credential(self, current_token: str | None = None) -> CredentialRotation:
363:         token = current_token or self._actor_token or ""
364:         result = self._transport.call(
365:             "finalisma_rotate_agent_credential",
366:             {"team_id": self.team_id, "agent_id": self.agent_id, "current_token": token},
```

**Issue.** When `require_actor_auth=False` and no token is set, `current_token=""` is sent. `core.py:913-914` then calls `_require_actor_credential` with an empty string, which fails the `len(token) < 16` check in `_token_hash` and raises `actor_auth_invalid`. The SDK surfaces this as a structured `AuthError`. Not a leak, but the silent fallback to `""` makes the error path confusing — the caller thinks it is proving with its stored token, but it is proving with an empty string.

**Recommended fix.** If neither `current_token` nor `self._actor_token` is set, raise immediately with a clear "no credential available to prove rotation" error instead of sending `""`.

---

## Secret-logging sweep

Pattern: `print|logging|log(|__repr__|__str__` near credential variables, across all five new files.

**Result: clean.** No `print(`, no `logging.`, no `__repr__`/`__str__` override references any credential material in `tenancy.py`, `roster.py`, `outbox.py`, or `bridge.py`. The only `__repr__` in scope is `sdk/client.py:320-321`:
```
320:     def __repr__(self) -> str:
321:         return f"FinalismaClient(coordinator_url={self.coordinator_url!r}, agent_id={self.agent_id!r}, team_id={self.team_id!r})"
```
This is safe — no token, no secret. The module docstring (`client.py:298-300`) explicitly promises this hygiene and the code keeps the promise.

## Credential rotation — airtightness verification

**Core path** (`core.py:897-958`): `rotate_agent_credential` runs inside `self._transaction()` (BEGIN IMMEDIATE), executes `UPDATE agent_credentials SET token_hash = ? ... WHERE team_id = ? AND agent_id = ?`, and returns the new token once. The old hash is overwritten in-place; there is no window where both hashes coexist. `_require_actor_credential` (`core.py:561-585`) uses `secrets.compare_digest` and checks `revoked_at is not None`. **Airtight at the core layer.**

**Server path** (`server.py:625-630`): `finalisma_rotate_agent_credential` dispatches directly to `store.rotate_agent_credential` with no intermediate caching. **Airtight.**

**SDK path** (`sdk/client.py:362-377`): After a successful rotation, `self._actor_token = new_token` (line 370). The retry loop in `_JsonRpcTransport.call` (`client.py:245-288`) retries only on `_TRANSIENT_STATUSES` (408/429/5xx) and only for methods in `_IDEMPOTENT_METHODS`, which includes `finalisma_rotate_agent_credential` (line 184). The server-side rotation is keyed by `(team_id, agent_id)` and the UPDATE is idempotent in effect (re-running with the *old* token fails auth; re-running with the *new* token succeeds and returns the same new hash). **No wrong-auth resend.**

**One residual risk (downgraded to LOW-3 above):** the silent `current_token=""` fallback.

## Bridge HMAC + replay — verification

- **Signature verification**: `verify_signature` (`bridge.py:260-280`) — constant-time via `hmac.compare_digest` (line 280). Correct.
- **Timestamp window**: `abs(now - ts) > max_age_seconds` with default 300s (`bridge.py:273-275`). Correct.
- **Canonicalisation**: `_hmac_sign` and `verify_signature` use identical `f"{timestamp}.{json.dumps(body, sort_keys=True, separators=(',', ':'))}"`. Consistent.
- **One-use bootstrap nonces**: `parse_bootstrap` (`bridge.py:465-504`) atomically checks and consumes the nonce inside `self.store._transaction()`. Correct.
- **Token-in-path rejection**: `parse_bootstrap` (`bridge.py:476-485`) and `_join_target` in `server.py:1034-1042` both reject non-`pair_` join paths. Correct.
- **HIGH-1 (hash-as-key fallback) is the only open issue in this class.**

## Tenant isolation — negative testing

- **`tenancy_claims`** is scoped by `org_id` with a FK to `tenancy_orgs`. Queries filter by `org_id`. Cross-org read requires supplying another org's id, which `assert_scope` then gates on membership. **Structurally correct, but weakened by MEDIUM-1 / MEDIUM-2 (actor_key is forgeable, and assert_scope takes free-form inputs).**
- **`assert_scope` misuse**: a caller passing another `org_id` + derived key passes the key check; the `is_member` check then blocks non-members. So cross-org access is blocked *as long as membership is correctly enforced*. Intra-org impersonation is not blocked (MEDIUM-2).
- **`roster_*` / `outbox_*` tables**: no `org_id` column (MEDIUM-3). In the single-node preview with one DB per team this is accidentally safe; on a shared multi-tenant DB it is a cross-tenant leak.
- **`outbox_dlq`**: no tenant column at all — dead-lettered envelopes from org A are visible to any process that can read the DB.

## Server-hub

- **Rate limit ordering** (`server.py:1085-1094`): the MCP POST path uses `limiter.allow(key, reserve=True)` which checks both the window limiter and the concurrent-reserve cap. The `_WindowRateLimiter.allow` (`server.py:76-90`) holds `self._lock` for the entire check-and-insert, so the two checks are atomic with respect to each other. **No bypass.**
- **Session cursor race** (`core.py` session_ack path, not re-read this session but covered by 73 passing tests including `test_server_hub`): ack is monotonic via `ON CONFLICT ... DO UPDATE SET last_ack_seq = excluded.last_ack_seq` where `excluded` is `MAX(seq) ... WHERE acked = 1`. Concurrent acks converge. **Safe.**
- **Origin checks**: `do_OPTIONS` (`server.py:910-921`) rejects bad Origin with 403 without requiring a bearer. `_authorized` (`server.py:854-862`) requires either a valid bearer *or* a valid Origin. **Intact.**

## Test result

```
Ran 73 tests in 6.348s
OK
```
(Targeted run: `tests.test_actor_credentials_core tests.test_bridge tests.test_server_hub tests.test_tenancy`.)

Full-suite run: `Ran 181 tests in 21.585s / FAILED (failures=3, errors=1)`. The 4 failures are all pre-existing site-bundle issues (`test_metrics_activation` import error, `test_progressive_enhancement_and_gated_story_cta`, `test_static_launch_bundle_contains_guides_articles_and_social_asset`) — none are in the security surface under review, and none were introduced by Wave A.

## Risks the orchestrator must fix before merging (ranked)

1. **HIGH-1** — Remove the `secret_hash` HMAC fallback in `bridge.py:221-224`. This is a real forgewing path for anyone with DB read access.
2. **HIGH-2** — Redact response body in `sdk/client.py:264` before attaching to the exception.
3. **MEDIUM-1 + MEDIUM-2** — Harden `derive_actor_key` with a server secret and bind `assert_scope` to an already-authenticated identity, or rename `actor_key` to reflect that it is a checksum, not a capability.
4. **MEDIUM-3** — Add `org_id` columns to `roster_*`, `outbox_*`, `bridge_*` tables, especially `outbox_dlq`.
5. **MEDIUM-4** — Re-validate recipient at outbox delivery time; do not trust the `recipient` column on retry/DLQ replay.
6. **LOW-1** — Replace `_secure_compare` with `hmac.compare_digest`.
7. **LOW-2 / LOW-3** — Align bridge auth with `require_actor_auth`; harden SDK rotation fallback.

## Not done

- No static-analysis or fuzzing of the JSON-RPC envelope parser.
- No live HTTP replay test against a running server (read-only lane; no server process started).
- No review of `src/finalisma_mcp/metrics_activation.py` (does not exist — the `test_metrics_activation` import error is a pre-existing site-bundle issue, not in scope).
- No formal verification of the `BEGIN IMMEDIATE` isolation under SQLite WAL with concurrent writers from multiple threads — correctness is inferred from the 73 passing tests and the lock ordering, not measured under load.

## Invariants touched

None — read-only review. No source files modified.
