# Weft security audit — 2026-08-15 (lane L7)

Read-only adversarial review of the recently changed attack surface, driven
against the real code path (live HTTP service, real SQLite-WAL backend, real
routes). Scope: `src/weft_sdk/client.py`, `src/weft_cloud/rooms.py`,
`src/weft_cloud/service.py`, `src/weft_cloud/identity/agent_keys.py`,
`src/weft_cloud/identity/orgs.py` — plus the commits that changed them
(`c9f4e0e` SDK, `1e0aa5a` REST parity, `d344ebe` signout/owner-gate).
Contract baseline: `docs/SECURITY_GATES.md`; style baseline:
`docs/SECURITY_REVIEW_2026-08-05.md`.

## Result summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 1 |
| Low      | 5 |
| Info     | 3 |

Every LOW+ finding below was exercised with a reproduction against the real
service (stdlib-only scratch scripts under the temp dir, never in the repo)
and is marked *REPRODUCED* or *SPECULATIVE*. Nothing found that lets one
tenant read or write another tenant's data, revoke another identity's
credential, or forge a room membership.

## CRITICAL / HIGH summary

None. The new authn/authz layers hold: identity-argument rejection on the new
REST routes, sender-scoped receipts, owner-gated `remove_member`, and the
`agk_` signout revocation were each attacked directly and each held.

## Detailed findings

### MEDIUM-1 — `message_kinds` list size is unbounded; ~33k entries overflow the SQLite bind limit and 500 on both surfaces

**Gate violated:** the v1-parity contract in `tests/test_webapp_v1_parity.py`
("neither surface may turn unvalidated client input into a 500") — the exact
class the `1e0aa5a` commit claimed to close for cursors, and the reason this
lane was asked "anything in rooms.py that still 500s on adversarial input".

**Evidence:** `rooms.py:158-176` `_validate_message_kinds` validates each
ENTRY (regex, ≤32 chars) but never bounds the LIST. `rooms.py:1392-1399`
then builds one `IN (?, ?, …)` clause with one placeholder per entry:

```
1392:            if kind_filter:
1393:                placeholders = ", ".join("?" for _ in kind_filter)
1394:                rows = tx.execute(
1395:                    "SELECT * FROM cloud_room_event_log WHERE tenant_id = ? AND room_id = ? "
1396:                    "AND seq > ? AND message_kind IN (" + placeholders + ") "
```

SQLite rejects a statement with more variables than
`SQLITE_MAX_VARIABLE_NUMBER` (32 766 in this Python build) with
`sqlite3.OperationalError: too many SQL variables`, which is not a
`RoomError`, so it escapes to the generic `except Exception` in
`service.py:1383-1386` → HTTP 500 `internal_error` (and to the
`internal_error` tool result on `/mcp`, `mcp.py:451-457`). `wait`
(`rooms.py:1436-1509`) reaches the same clause through `poll`, so
`/v1/rooms/wait` and MCP `room_wait` fail identically.

**Attack scenario (REPRODUCED).** A room member POSTs
`/v1/rooms/poll` with `message_kinds` = 40 000 distinct valid slugs
(`kind00000` … `kind39999`, a ~480 KB body — well under the 1 MB
`_read_body` cap). Result: `HTTP 500 {"error":{"code":"internal_error"}}`,
deterministic, on every poll/wait with that body. No data is leaked (the
generic handler returns a fixed body) and the process survives, but any
client that keeps the filter set can no longer read the room through either
surface — a self-inflicted or attacker-inflicted availability hole that is a
pure input-validation gap. `room_send` is unaffected (its routing path never
builds an IN clause), so an attacker can keep the room working while their
victim's filtered polls 500.

**Recommended fix.** Bound the filter in `_validate_message_kinds`
(e.g. reject `len(value) > 64` with `invalid_argument`, or slice to the cap
before building the clause). One-line class fix, mirrors the existing
`_IDEMPOTENCY_KEY_MAX_LEN` / `entry_ids ≤ 200` precedent.

---

### LOW-1 — Non-string container `room_id` (list/dict) 500s every /v1 room route

**Evidence:** `service.py:532-547` `_room_tenant` and
`rooms.py:594-623` `_resolve_room_tenant` bind `room_id` into
`WHERE room_id = ?` with no type check. A JSON array/object as `room_id`
raises `sqlite3.ProgrammingError` on bind, which only `_handle`'s generic
handler catches → 500.

**Attack scenario (REPRODUCED).** `POST /v1/rooms/poll {"room_id": ["x"],
"after_seq": 0}` → HTTP 500 `internal_error`; `{"room_id": {"a":1}}` → 500.
Applies to every room route (poll, wait, send, receipts, ack,
remove_member, event_log, …). Member-only (auth still precedes the bind), no
leak, no crash — but this is the same "unvalidated input → 500" class the
parity contract forbids, and it is the first thing a fuzzer finds. (An int
`room_id` does NOT 500 — it misses the lookup and returns the uniform 404,
which is the desired no-oracle behaviour.)

**Recommended fix.** Type-check `room_id` (str, non-empty, matching the
`room_` id shape) at the top of `_room_tenant` before the DB call, raising
`invalid_argument` 400.

---

### LOW-2 — Non-string container `member_id` on `remove_member` 500s

**Evidence:** `rooms.py:1207-1211` binds `target_agent_id` directly into
`WHERE agent_id = ?`. No type check anywhere between the REST body
(`service.py:921-938`) and the bind.

**Attack scenario (REPRODUCED).** `POST /v1/rooms/remove_member
{"room_id": <real>, "member_id": ["x"]}` (or `{"a":1}`) → HTTP 500.
Owner-only surface, so the attacker must already own the room — availability
noise only, no authz bypass. The MCP surface maps the same input to a
structured `internal_error` tool result instead of a 500, confirming the
REST layer is the weaker of the two here.

**Recommended fix.** `if not isinstance(member_id, str) or not member_id.strip():
raise RoomError("invalid_argument", …)` at the top of `remove_member`.

---

### LOW-3 — `target_spec` int/bool 500s `room_send`

**Evidence:** `rooms.py:765-768` and `788-791` do
`specs = list(target_spec or [])` for anything that is not a string.
`list(42)` / `list(True)` raise `TypeError`, which escapes as a 500 on the
REST surface.

**Attack scenario (REPRODUCED).** `POST /v1/rooms/send {"room_id": <real>,
"target_spec": 42, "payload": "x"}` → HTTP 500. A dict target_spec does not
500 (its keys become specs — harmless), and a list of strings is fine, but
int/bool/float are a one-request 500 for any member.

**Recommended fix.** In `_route_targets` (or at the send boundary), require
`target_spec` to be a string or a list of strings:
`RoomError("invalid_argument", …)` otherwise.

---

### LOW-4 — Attacker-controlled strings reflected verbatim into error bodies (no amplification, but unbounded)

Two spots, both input-bounded (~1 MB request cap) and 1:1, so this is
reflection abuse, not an amplification gadget:

1. `rooms.py:800-805` `_reject_unroutable_specs` joins every unrouted spec
   into the 422 message: `f"Recipient(s) are not members of this room:
   {', '.join(unrouted)}"`. **REPRODUCED:** 5 000 bogus targets → a 422 with a
   ~70 KB error body; a 1 MB body of short specs → a ~1 MB error body.
2. `rooms.py:1596-1606` `receipts` echoes each requested `entry_id` back in
   the `not_found` entry. **REPRODUCED:** one 200 000-char entry_id →
   200 with a ~200 KB body.

No other member's data is involved in either path (only the caller's own
strings return). The risk is response-size abuse and the endpoint doubling as
a free reflector of arbitrary text. An org member could use the receipts echo
to have the service return attacker-written content in an authenticated
context.

**Recommended fix.** Cap `target_spec` list length (e.g. 64) and truncate
the `unrouted` join in the message (e.g. first 10 + count); cap `entry_id`
length (e.g. 512) in `receipts` validation.

---

### LOW-5 — SDK `rotate_credential` bypasses the hosted-mode identity-arg stripping (SPECULATIVE — server backstop holds today)

**Evidence:** `client.py:481-495` `_call` is the only method that strips the
seven `_HOSTED_FORBIDDEN_IDENTITY_ARGS` in hosted mode. `rotate_credential`
(`client.py:527-532`) calls `self._transport.call` directly with
`{"team_id": …, "agent_id": …, "current_token": token}` — team_id/agent_id
reach the hosted `/mcp` wire un-stripped, contradicting the class docstring
promise ("never injects identity arguments … in hosted mode").

**Why it is not exploitable today:** the hosted dispatcher exposes only the
12 room tools (`mcp.py` `HOSTED_TOOLS`) and rejects client identity args
anyway, so the call fails closed with an unknown-tool / invalid-argument
error. **SPECULATIVE:** if a future hosted tool accepts `team_id`/`agent_id`
or a `rotate`-style tool is added to `HOSTED_TOOLS`, the SDK would silently
start sending identity arguments in hosted mode, and the server-side
rejection would be the only line of defence — exactly the confused-deputy
shape the stripping was built to prevent.

**Recommended fix.** Route `rotate_credential` through `_call` (which is
what strips in hosted mode), or make `_call` strip `current_token`/team_id
explicitly and have `rotate_credential` use it.

---

### INFO-1 — Timing on the new `revoke_by_token_hash` path: nothing found

Checked the specific lane brief item. `agent_keys.revoke_by_token_hash`
(`agent_keys.py:163-192`) selects on `token_hash` (SHA-256 of a token the
caller presented and already authenticated with), over a UNIQUE indexed
column (`migrations.py:210,219`). The caller cannot vary the compared
material (it must present a valid credential to reach the revoke at all —
`service.py:434-454` authenticates first), and unknown/malformed credentials
exit with the byte-identical 401 before any revocation work. No timing
oracle: nothing found. The one structural nicety worth keeping: the
SELECT-then-UPDATE rowcount gate (`agent_keys.py:176-192`) releases room
seats only when THIS call flipped the key, so a re-revoke can never
double-decrement member counters.

### INFO-2 — `groups` accepts a non-string `group_name` (type confusion, not exploitable)

`rooms.py:1784-1820` never type-checks `group_name`. **REPRODUCED:**
`group_name: 42` returns 200 and stores an INTEGER group name (SQLite is
dynamically typed). Because `_route_targets` compares against names with `in`
(`rooms.py:759`), a JSON string `"42"` can never address that group and the
group can never be routed to by any JSON name — the group is permanently
unaddressable, not attackable. Recommend a `isinstance(group_name, str)`
check for hygiene (and parity with `_validate_message_kind`).

### INFO-3 — `_ORG_BOOTSTRAP_PASSWORD` is a fixed, in-repo password for `add_member`-provisioned accounts (SPECULATIVE — no HTTP route reaches it)

`orgs.py:42` hardcodes `"CorrectHorse-Battery-Staple!42"` and
`add_member` (`orgs.py:94-96`) provisions accounts with it, `email_verified=1`.
Anyone who reads the source knows the password of every account created this
way, and the service surface has no change-password route in the routes table
(`service.py:1451-1474`). **Verified:** `add_member` is NOT reachable over
HTTP today — no /v1 route, no hosted MCP tool (grep confirms callers are
tests only), and the email-locked invite flow (`invites.accept`, which lets
the accepter set their own password) is the real onboarding path. **SPECULATIVE:**
if an org-admin endpoint for `add_member` is ever wired, every provisioned
account is take-overable by anyone with repo access until this constant is
removed. Recommend deleting the constant and requiring a password (or a
reset link) in `add_member` before any surface exposes it.

## Verified-safe list (attacked and held)

- **Signout kills the presented `agk_` key, not the session, not anyone
  else's.** REPRODUCED: signout with a key → 200, then the key gets 401 on
  its next call; signout with one session leaves the account's other session
  alive; a bogus `fss_`/`agk_` token gets the byte-identical 401 — no
  credential-type or existence oracle.
- **Receipts are sender-scoped; no IDOR.** REPRODUCED: member B querying the
  room owner's `entry_id` gets `not_found` (not the outbox status); the
  owner gets `queued`. A non-member / fabricated room gets the uniform 404
  `room_not_found` — no room-existence oracle.
- **`remove_member` is owner-gated end-to-end.** REPRODUCED: member → 403
  `owner_required`; owner → 200 + seat released; owner removing themselves →
  403; non-member caller → uniform 404; unknown member → 404
  `member_not_found` (no member-existence oracle for non-owners).
- **Identity args are rejected on the new REST routes.** REPRODUCED:
  `agent_id` / `owner_agent_id` / `tenant_id` in the body of
  `/v1/rooms/receipts` and `/v1/rooms/remove_member` → 400 `invalid_argument`
  (server-side; the SDK also strips them in hosted mode before they travel).
- **Cursor/seq/limit validation on both surfaces.** REPRODUCED: `after_seq`
  bool → 400, string → 400, negative → 400, beyond head → 400
  `invalid_cursor`; `seq` bool/dict/negative/1e30 → 400; `limit` clamps to
  1..200. `timeout_seconds` of any non-int coerces to the 20 s default.
  These are the `1e0aa5a` fixes — they hold.
- **SDK 429 handling preserves the structured error without leaking the
  body.** `client.py:340-377` adopts only a validated snake_case `code` and
  numeric `retry_after`; the server message is never echoed (asserted in
  `tests/test_sdk.py:546-549`, 25 sdk + 30 sibling tests green).
- **`add_member` owner-gate holds at both layers.** `orgs.py:77-104`:
  `role="owner"` requires an owner at layer 1 (`ctx.require_role`) AND layer
  2 (`require_db_role`); covered by
  `test_admin_cannot_add_member_with_owner_role` (green).
- **Key revocation releases room seats in the same transaction**
  (`agent_keys.py:152-160` → `rooms.py:338-403`), gated on the rowcount so a
  re-revoke is idempotent; signout's single-transaction shape avoids the
  nested-BEGIN deadlock documented in `sessions.py:89-97`.
- **No secret in error responses**: `_handle` maps every unexpected
  exception to a fixed `internal_error` body (`service.py:1383-1386`); the
  `log_message` handler never logs bodies or tokens.

## Test result

Targeted suites run this session (all green):

```
tests.test_webapp_v1_parity tests.test_identity_agent_keys tests.test_identity_orgs_roles   Ran 48 tests  OK
tests.test_sdk tests.test_interop_sdk                                                       Ran 25 tests  OK
tests.test_webapp_v1_parity tests.test_hosted_mcp tests.test_agent_keys_service
  tests.test_auth_rate_limits                                                               Ran 68 tests  OK
```

Reproduction harness: `l7_repro.py`, `l7_type_probe.py` (stdlib-only, temp
dir, real HTTP service on an ephemeral port, torn down inside the script).
The three type-confusion 500s and the 40k-`message_kinds` 500 were each
observed against the live routes, not inferred from the code.

## Risks the orchestrator should fix before merge (ranked)

1. **MEDIUM-1** — cap `message_kinds` in `_validate_message_kinds`
   (`rooms.py:158-176`) so a large filter cannot overflow the SQLite bind
   limit and 500 poll/wait on both surfaces.
2. **LOW-1..3** — type-check `room_id`, `member_id`, and `target_spec` at the
   service boundary; these are the remaining members of the "adversarial
   input → 500" family the parity contract forbids.
3. **LOW-4** — bound the `unrouted` join and `entry_id` lengths.
4. **LOW-5** — send `rotate_credential` through the hosted-mode stripping
   path.
5. **INFO-3** — delete `_ORG_BOOTSTRAP_PASSWORD` before any surface wires
   `add_member`.

## Not done

- No fuzzing of the JSON-RPC envelope parser beyond the typed probes above.
- No static timing measurement (Python-level); the timing conclusions are
  from code structure (hash-indexed lookups, auth-before-revoke ordering).
- No load test of `_wait_slots` contention across the REST + MCP surfaces
  (both share the one semaphore; the bound is correct by inspection and by
  existing tests).
- Full-suite run not performed; the four suites above are the audited
  surface plus its direct collateral.

## Invariants touched

None — read-only review. One new file written: this one. No source files
modified, no commits made, no test edits.

## Remediation 2026-08-15 (lane L11 — input hardening, TDD)

- **MEDIUM-1 — FIXED.** `_validate_message_kinds` now caps the list at 64
  (`_MESSAGE_KINDS_MAX_ITEMS`, `rooms.py`) and raises `invalid_argument` 400
  beyond it, before any `IN (…)` clause is built. Guard tests:
  `test_poll_rejects_oversized_message_kinds_with_400`,
  `test_wait_rejects_oversized_message_kinds_with_400`,
  `test_mcp_poll_rejects_oversized_message_kinds_with_invalid_argument`
  (40 000 kinds → 400 on REST, `invalid_argument` on MCP — never 500), plus
  boundary pins `test_poll_allows_64_message_kinds` /
  `test_poll_rejects_65_message_kinds_with_400` (`tests/test_input_hardening.py`).
- **LOW-1 — FIXED.** `room_id` is type-checked (non-empty string) in
  `service.py:_room_tenant` and via the new `_validate_room_id` at the top of
  `rooms.py:_resolve_room_tenant` and `_resolve_room_for_link`, so every /v1
  room route (incl. join) and the MCP surface return `invalid_argument` 400
  instead of 500. Guard tests: `test_non_string_room_id_400_on_every_room_route`,
  `test_join_with_non_string_room_id_400`,
  `test_mcp_poll_with_list_room_id_is_invalid_argument`.
- **LOW-2 — FIXED.** `remove_member` type-checks `target_agent_id` after the
  owner gate and before the DB bind. Guard tests:
  `test_remove_member_rejects_non_string_member_id_with_400`,
  `test_mcp_remove_member_with_list_member_id_is_invalid_argument`; the
  existing `member_not_found` 404 for unknown string ids is pinned by
  `test_remove_member_with_unknown_string_member_id_still_404`.
- **LOW-3 — FIXED.** New `_normalize_target_spec` (`rooms.py`) requires
  `target_spec` to be a string or a bounded list of strings; int/bool/float
  and dicts now raise `invalid_argument` 400 on both surfaces instead of
  `TypeError` → 500 / `internal_error`. Guard tests:
  `test_send_rejects_int_bool_float_target_spec_with_400`,
  `test_send_rejects_dict_target_spec_with_400`,
  `test_mcp_send_with_int_target_spec_is_invalid_argument`; legitimate shapes
  pinned by `test_send_allows_string_and_list_of_strings`.
- **LOW-4 — FIXED (bounded, via the existing vocabulary).** The reflection
  surfaces are now bounded with type + length caps that reuse `invalid_argument`:
  `target_spec` ≤ 64 entries × ≤ 128 chars (bounds the `unrouted` join in the
  422 message) and `entry_ids` ≤ 512 chars each (bounds the receipts echo).
  Guard tests: `test_send_rejects_oversized_target_list_with_400`,
  `test_send_rejects_overlong_target_entry_with_400`,
  `test_receipts_rejects_overlong_entry_id_with_400`,
  `test_receipts_accepts_512_char_entry_id_as_not_found`. The audit's
  "first 10 + count" join truncation was NOT added: with the caps the
  worst-case message is ~8 KB, so the truncation would be redundant; noted
  here rather than inventing a half-measure.
- **LOW-5 / INFO-2 / INFO-3 — left untouched (out of lane scope).** LOW-5 is
  the SDK (`src/weft_sdk/client.py`), not owned by this lane; INFO-2/INFO-3
  were not in the lane's findings list.

Verification: `tests.test_input_hardening tests.test_webapp_v1_parity
tests.test_webapp_rooms tests.test_hosted_mcp` — 96 tests OK, run twice; plus
164 tests OK across the room/cloud/quota/security collateral suites. All
guard tests were observed RED against the pre-fix code before the fixes.
