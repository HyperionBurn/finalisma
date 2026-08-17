# Hosted agent keys

Hosted Weft agent keys are long-lived bearer credentials for non-interactive
REST clients and remote-mode MCP bridge clients. They are separate from browser sessions and from the legacy
self-hosted actor and pairing credentials.

## Credential planes

| Prefix | Plane | Purpose | Storage contract |
| --- | --- | --- | --- |
| `agk_` | Hosted cloud identity | Long-lived agent key for client configuration | Raw token is returned once; only its SHA-256 hash is stored. |
| `fss_` | Hosted cloud identity | Interactive browser/API session | Session credential with expiry/revocation; an unexpired session may be rotated once; raw value is not recoverable from storage. |
| `fst_actor_` | Legacy self-hosted MCP | Local actor credential | Self-hosted only; not a hosted cloud credential. |
| `fst_pair` / `fst_session` | Legacy self-hosted pairing | Local pairing/session credentials | Separate from hosted `agk_` and `fss_` credentials. |

Do not present the legacy `fst_*` credentials as aliases for hosted `agk_` keys.

## Hosted `agk_` contract

- An authenticated `fss_` session creates, lists, labels, and revokes keys.
  An `agk_` key cannot manage other keys.
- Creation returns the raw `agk_...` token exactly once. Listing returns
  metadata only and never the raw token or stored hash. Lost keys require a
  new key.
- Keys have no expiry column. They remain usable until revoked or until the
  account no longer has valid membership in the key's tenant.
- Validation re-derives the current tenant membership and role on every
  request. Demotion therefore takes effect on the next request.
- Password reset revokes the account's active sessions and agent keys, and
  releases those keys' active room seats in the same transaction.
- Bulk key revocation follows the same seat-release rule; historical room
  events remain attributed to the revoked identity, while a replacement key
  can take the freed capacity.
- Admin membership removal revokes the account's keys for that tenant in the
  same transaction as membership deletion. Re-adding the account does not
  resurrect those keys; a new key is required. Keys in another tenant are not
  revoked by a single-tenant removal.
- Unknown, tampered, malformed, and revoked hosted credentials use the shared
  invalid-session refusal path; callers must not use errors to distinguish
  which credential state failed.

## Identity boundary

An `agk_` key authenticates the account that owns it and derives a distinct,
stable room identity from its server-side key id. Multiple keys for one
account therefore create separate hosted agent identities while preserving
the same account-level ownership and role. Client-supplied
`agent_id`, `tenant_id`, `team_id`, and sender/owner identity arguments are not
an override for the authenticated account.

## Hosted MCP versus stdio

Hosted MCP is the Streamable HTTP endpoint `POST /mcp` and accepts a hosted `fss_` session or `agk_` key.
The hosted dispatcher exposes the cloud room tools currently listed by
`tools/list`; it intentionally does not expose the self-hosted registration,
pairing, task, roster, outbox, or tenancy surface. Link revocation is currently
available through the hosted REST route, not the hosted MCP tool list.

The stdio bridge is a separate local process boundary, not a hosted stdio endpoint. It can forward a hosted
bearer to the remote `/mcp` endpoint when configured for remote mode, but the
legacy self-hosted actor/pairing credentials are not interchangeable with
hosted cloud credentials.

## Claims this document does not make

This contract does not promise universal third-party MCP-host compatibility,
SSO/OIDC, enterprise compliance, an SLA, or production-data readiness. Those
claims require separate evidence.
