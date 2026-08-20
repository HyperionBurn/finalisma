# Weft activation metrics

Local-first, stdlib-only, SQLite-backed instrumentation for the activation
funnel. No external analytics vendor, no PII.

## Activation funnel

```
link_created → link_previewed → link_accepted → first_task_claimed → first_evidence_verified
```

**Headline metric:** `ttfvh_ms` — time-to-first-verified-handoff, measured from
`link_created_at` to `first_evidence_verified_at` for the same workspace.

## Module: `weft_mcp.metrics_activation`

| Function | Purpose |
|---|---|
| `init(db_path)` | Idempotent schema setup. Call at server startup. |
| `record_event(team_id, agent_id, event_type, metadata=None)` | Record an activation event. |
| `derive_ttfvh(team_id, workspace_id) -> int \| None` | Compute ttfvh in ms, or `None` if incomplete. |
| `funnel_snapshot(team_id) -> {...}` | Per-stage counts + conversion deltas. |
| `weekly_retention(team_id, week_start) -> {...}` | Workspaces with ≥1 evidence-gated handoff in the week. |
| `handoffs_per_workspace(team_id) -> {...}` | Evidence-verified handoffs per active workspace. |
| `invite_to_activated_conversion(team_id) -> {...}` | Ratio of invites that reached evidence-verified. |
| `record_from_store_event(store_event) -> str \| None` | Explicit compatibility seam for direct callers. The dispatcher wires this automatically. |

## Schema

Tables are prefixed `metrics_` and live in the same SQLite file as the core
store:

- `metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json)`
- `metrics_funnels(team_id, workspace_id, link_created_at, ..., ttfvh_ms)` — a materialized per-workspace snapshot refreshed in the same transaction.

Events are written with WAL + `BEGIN IMMEDIATE`; timestamps are UTC ISO-8601.

## No-PII contract

- Events carry `team_id` and `agent_id` only — no email, IP, name, phone,
  user-agent, or geo columns exist in the schema.
- `metadata_json` is caller-controlled. The module rejects top-level keys
  named `email`, `ip_address`, `ip`, `name`, `phone`, `user_agent`, `geo`,
  or `location`. This is a best-effort guard, not a substitute for caller
  discipline.
- Callers MUST NOT put PII into `metadata_json`.

## Automatic dispatcher integration

`WeftDispatcher` initializes the metrics schema and attaches a same-connection
observer to `WeftStore`. Relevant core events and their activation rows commit
or roll back together. The observer preserves the core event timestamp and
stores only a bounded workspace identifier, source event type, and short source
event hash. It does not copy task paths, session tokens, fencing tokens, or
quality payloads into the metrics table.

`record_from_store_event(store_event)` remains available for direct callers.
Pass the existing SQLite connection when the caller is already inside a
transaction. Without a connection, the function opens its own compatibility
transaction.

Mapping from core store events to activation events:

| Core store event | Activation event |
|---|---|
| `pairing.issued` | `link_created` |
| `pairing.previewed` | `link_previewed` |
| `pairing.joined` | `link_accepted` |
| `task.claimed` | `first_task_claimed` |
| `quality.evaluated` with `status=passed` | `first_evidence_verified` |

Legacy aliases `pairing.created`, `pairing.accepted`, and `task.verified` remain
accepted by the compatibility seam. Failed quality evaluations never produce
`first_evidence_verified`. Repeated observations of the same source object are
idempotent for the corresponding first-stage metric.

When a lifecycle operation does not provide a valid `workspace_id`, automatic
events use `team:<team_id>` as a deterministic aggregate workspace. Callers
that need per-workspace ttfvh must provide the same bounded workspace ID in
pairing and task metadata.

Unknown store event types are silently dropped (returns `None`).

## YC-readiness metric coverage

`docs/YC_READINESS.md` names eight metrics that must exist before fundraising
claims. Mapping to this module:

| # | YC metric | Function / query | Status |
|---|---|---|---|
| 1 | link-created → link-accepted conversion | `funnel_snapshot(team_id)["deltas"]["preview_to_accept"]` + `accept_to_claim` | ✅ computable |
| 2 | median and p95 time-to-first-verified-handoff | `derive_ttfvh(team_id, workspace_id)` per workspace; aggregate in dashboard | ✅ computable (per-workspace; aggregate externally) |
| 3 | weekly retained workspaces (≥1 evidence-gated handoff) | `weekly_retention(team_id, week_start)` | ✅ computable |
| 4 | sessions and evidence-gated handoffs per active workspace | `handoffs_per_workspace(team_id)` | ✅ computable |
| 5 | invite-to-activated-workspace conversion | `invite_to_activated_conversion(team_id)` | ✅ computable |
| 6 | provider/error cost per evidence-gated handoff | — | ⚠️ seam — this module does not see provider cost. The orchestrator must join `metrics_events` (event count) with provider billing data from the execution layer. |
| 7 | task failure, replay, and reconnect rates | — | ⚠️ partial — `metrics_events` records `first_evidence_verified` (success) but failure/replay/reconnect data lives in `core.py`'s `events` table. A join on `team_id` + time window is required. |
| 8 | paid conversion and gross margin by workspace | — | ⚠️ seam — payment data is outside this module's scope. The orchestrator must join workspace activation state with billing records. |

### Seams to close before fundraising claims

- **Provider cost (#6):** join `metrics_events` with the execution layer's
  token/cost log on `team_id` + `agent_id` + time window.
- **Failure/replay/reconnect (#7):** join `metrics_events` with
  `core.py`'s `events` table (`task.lease_expired`, `task.claim_conflict`,
  session reconnect events).
- **Paid conversion (#8):** join workspace activation state with the billing
  system's subscription records.

All five "computable" metrics above are fully served by this module alone.
