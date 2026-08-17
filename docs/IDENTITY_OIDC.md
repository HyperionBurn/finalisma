# Identity, Tenancy, and the OAuth/OIDC Seam

This document describes how Weft's **today** identity model (SHA-256
actor tokens issued once, stored only as hashes) maps to a hosted
**tomorrow** with OAuth/OIDC in front, and how the tenancy boundary keeps
orgs isolated when that future arrives.

## Today: actor credentials in `core.py`

From `src/weft_mcp/core.py` (schema v3):

- `register_agent` issues a raw `actor_token` **once** to a new
  identity. Only its SHA-256 hash is stored in `agent_credentials.token_hash`
  (a 64-char hex, `CHECK(length(token_hash) = 64)`). Existing identities
  never receive it again.
- `rotate_agent_credential` atomically replaces the hash and
  invalidates the old value. `rotation_count` is persisted.
- `join_pairing` lets a genuinely new invited identity bootstrap
  its credential through a one-time pair token; an already-registered
  invitee must supply its current `actor_token`, so a pair token cannot
  overwrite an existing identity.
- Every protected tool (`create_task`, `claim_task`,
  `update_task`, `verify_task`,
  `send_message`, `create_pairing`, `team_status`,
  `heartbeat`, the `session_*` tools) takes an optional
  `actor_token` and validates it against the stored hash via
  `secrets.compare_digest`.

The model is **capability-based**: the token is a bearer secret, not a claim
about who the user is.

## Tomorrow: OAuth/OIDC in front of tenancy

In the hosted multi-tenant path, an OAuth/OIDC resource server (e.g. a
gateway, Envoy filter, or ASGI/Starlette middleware) sits **in front of**
`src/weft_mcp/tenancy.py`. The seam contract is:

```
+----------------+       +------------------+       +------------------+
| OAuth/OIDC     |       | tenancy.py       |       | core.py          |
| resource server| ----> | (org_id,         | ----> | (team_id,        |
|                |       |  agent_id)       |       |  agent_id)       |
+----------------+       +------------------+       +------------------+
   validates               derives actor           validates against
   external JWT,           key, checks             agent_credentials
   maps sub ->             membership,             token_hash
   (org_id, agent_id)      enforces scope
```

**The rule:** `tenancy.py` functions take `org_id` + `agent_id` only. They
never see raw OAuth tokens, JWTs, or refresh tokens. The resource server is
responsible for:

1. Validating the external access token (signature, `exp`, `aud`, `iss`).
2. Extracting the `sub` (and optionally `org` claim) and mapping it to an
   internal `(org_id, agent_id)` pair — via a local mapping table or a
   deterministic derivation.
3. Passing only `org_id` + `agent_id` into tenancy.

This keeps the tenancy layer portable across IdPs (Auth0, Okta, Keycloak,
Google, Azure AD) because it depends on no OAuth library.

### Mapping external `sub` to internal actor key

The internal per-org, per-agent key is:

```
actor_key = SHA-256(org_id + agent_id)
```

This is exactly the construction stored in `tenancy_claims.actor_key` and
checked by `tenancy.assert_scope`. To bridge OAuth:

- **Option A (recommended):** persist a `tenancy_identity_map(external_iss,
  external_sub, org_id, agent_id)` table. On first login, an admin links the
  external identity to an org+agent; thereafter the resource server looks it
  up and calls `tenancy.assert_scope(db, org_id, agent_id,
  tenancy.derive_actor_key(org_id, agent_id))`.
- **Option B (fully deterministic, no mapping table):** derive
  `agent_id = "ext_" + SHA-256(sub)[:16]` and `org_id` from a verified
  `org` claim in the JWT. This is stateless but couples the IdP's `org`
  claim format to Weft's org IDs.

Option A is preferred because it lets operators rotate IdPs, merge orgs, and
re-link identities without re-deriving IDs.

## Tenancy isolation model

`tenancy.py` manages three tables, all prefixed `tenancy_` and coexisting
with `core.py`'s tables in the same SQLite file:

| Table | Purpose |
| --- | --- |
| `tenancy_orgs` | Org ID, name, creation time |
| `tenancy_org_members` | `(org_id, agent_id, role, joined_at)` — membership |
| `tenancy_claims` | `(org_id, resource_type, resource_id, actor_key)` — ownership |

Isolation invariants:

- Every tenancy function takes `org_id` as a first-class argument. There is
  no "current org" global state.
- `assert_scope(db, org_id, agent_id, actor_key_hex)` validates **both**
  that `actor_key_hex == SHA-256(org_id + agent_id)` **and** that the agent
  is a member of the org. A valid key for org A is useless in org B.
- `tenancy_claims.actor_key` stores only the derived SHA-256 — no plaintext
  token material — so a leaked claims table does not reveal credentials.
- Membership is per-org: the same `agent_id` in two orgs is two distinct
  membership rows with independent keys.

### Multi-tenant data boundary

The single-node SQLite coordinator is the current boundary (see
`docs/SECURITY_GATES.md`, "Must pass before multi-instance hosted traffic").
To enforce tenant isolation at the data layer:

1. **Every query filters by `org_id`** — there is no un-scoped read or
   write path in `tenancy.py`.
2. **Foreign keys cascade**: `tenancy_org_members` and `tenancy_claims`
   reference `tenancy_orgs(org_id) ON DELETE CASCADE`, so deleting an org
   removes its members and claims.
3. **No cross-org joins**: tenancy functions never join across orgs. A
   caller that wants cross-org visibility must do so explicitly with
   separate, audited calls.
4. **Future sharding key**: `org_id` is the natural tenant shard key for
  -tenancy tables when the data layer moves to Postgres/TiDB/CockroachDB
  for horizontal scaling.

## Token rotation and compromise response

### Actor tokens (today, in `core.py`)

- `rotate_agent_credential(team_id, agent_id, current_token)`
  atomically replaces the hash, bumps `rotation_count`, and clears
  `revoked_at`. The old token stops working immediately.
- In `--actor-auth required` mode, every protected call re-validates the
  token, so a rotated token has no lingering window.
- A v2→v3 migration does **not** fabricate credentials for old rows; an
  operator must bootstrap through a trusted local rotation call.

### Per-org actor keys (in `tenancy.py`)

Because `derive_actor_key(org_id, agent_id)` is a pure function of the
org+agent pair, "rotation" means changing the agent's identity within the
org:

- **Compromise of a single agent:** remove it from the org
  (`remove_member`) and re-add under a new `agent_id`. The old derived key
  is no longer valid because membership is gone.
- **Compromise of the whole org:** delete the org
  (`DELETE FROM tenancy_orgs WHERE org_id = ?`) — cascading FKs wipe members
  and claims. Re-create the org and re-link identities.
- **IdP-level compromise (OAuth future):** the resource server revokes the
  external tokens at the IdP. Because `tenancy.py` never sees the external
  token, the internal `actor_key` is unaffected — but the mapping between
  external `sub` and internal `(org_id, agent_id)` must be audited and
  re-issued. Option A's `tenancy_identity_map` table supports this with a
  `linked_at` + `revoked_at` column.

### Recommended compromise-playbook steps

1. Identify the scope: single agent, whole org, or IdP.
2. For a single agent: `remove_member` + re-add under new `agent_id`, then
   call `rotate_agent_credential` for the corresponding
   `(team_id, agent_id)` in `core.py`.
3. For a whole org: delete the org row (cascade), recreate, re-link all
   members, rotate all corresponding actor credentials.
4. For IdP compromise: revoke at the IdP, mark `tenancy_identity_map` rows
   as revoked, force re-linking on next login, and rotate any actor
   credentials whose `agent_id` was reachable from the compromised
   mapping.
5. Audit: every rotation and membership change should emit an event
   (`core.py` already emits `agent.credential_rotated`,
   `agent.credential_issued`); tenancy should do the same when it gains an
   event table.

## API surface of `tenancy.py`

| Function | Purpose |
| --- | --- |
| `init(db_path)` | Idempotent; creates `tenancy_*` tables |
| `create_org(db_path, name) -> org_id` | Org CRUD |
| `list_orgs(db_path) -> [org]` | Org CRUD |
| `add_member(db_path, org_id, agent_id, role)` | Membership |
| `remove_member(db_path, org_id, agent_id)` | Membership |
| `is_member(db_path, org_id, agent_id) -> bool` | Membership |
| `list_members(db_path, org_id) -> [member]` | Membership |
| `derive_actor_key(org_id, agent_id) -> sha256_hex` | Key derivation |
| `assert_scope(db_path, org_id, agent_id, actor_key_hex)` | Per-request gate |
| `claim_resource(db, org_id, resource_type, resource_id, actor_id)` | Ownership |
| `list_claims(db, org_id) -> [claim]` | Ownership |

All functions take `db_path` (no global state) and `org_id` (tenant shard
key). The module depends on nothing outside the Python standard library and
does not import `core.py`, so there is no circular dependency and the
boundary is mechanically enforceable.
