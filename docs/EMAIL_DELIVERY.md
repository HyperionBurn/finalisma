# Email delivery — Stage 2 (SMTP drain worker)

Outbound mail used to be a dead end: signup verification, password reset, and
org invites each wrote a durable row to `cloud_identity_outbox` and stopped.
This document describes the Stage-2 worker that turns those rows into actually
delivered email.

**Plain statement up front: without SMTP configured, mail stays in
`cloud_identity_outbox` undelivered.** The worker refuses to drain when SMTP is
disabled — it exits cleanly instead, and every row remains `queued`. That is
the deliberate default so local development and the test suite never need a
mail server. The moment real SMTP values are set in the environment, delivery
works with no code change.

## How it fits together

1. **Callers are unchanged.** `accounts.signup`, `accounts.request_password_reset`
   and `invites.create` keep writing a durable `cloud_identity_outbox` row via
   `LocalOutboxMailer` (Stage 1). A signup is never lost because mail failed.
2. **The worker drains the outbox.** `OutboxDrainer`
   (`src/finalisma_cloud/identity/outbox_worker.py`) claims undelivered rows
   and sends each through `SmtpMailer` (STARTTLS + AUTH, standard library
   only), marking every row `sent` or `failed`.
3. **Selection is by environment.** `mailer.build_mailer(backend)` returns
   `SmtpMailer` when `FINALISMA_SMTP_HOST` is set and `LocalOutboxMailer`
   otherwise. The worker only ever drains with an SMTP mailer — it can never
   re-enqueue a row into the outbox it is draining.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `FINALISMA_SMTP_HOST` | — (SMTP disabled) | SMTP relay host. Setting this enables delivery. |
| `FINALISMA_SMTP_PORT` | `587` | Submission port (STARTTLS). |
| `FINALISMA_SMTP_USERNAME` | — | Auth username (required once the host is set). |
| `FINALISMA_SMTP_PASSWORD` | — | Auth password (required once the host is set). |
| `FINALISMA_SMTP_FROM` | — | From address, e.g. `no-reply@example.com` (required once the host is set). |
| `FINALISMA_DB_PATH` | `./data/finalisma-cloud.db` | The **same** SQLite file the web/service processes write to. |
| `FINALISMA_DRAIN_INTERVAL` | `5` | Seconds between passes. |
| `FINALISMA_DRAIN_BATCH` | `50` | Max rows claimed per pass. |
| `FINALISMA_DRAIN_MAX_ATTEMPTS` | `5` | Retries before a transient failure becomes terminal. |
| `FINALISMA_DRAIN_BACKOFF` | `60` | Base seconds between retries (grows linearly per attempt). |

Validation follows the service launchers: a half-set SMTP configuration
(host without username/password/from, or a malformed port/interval) raises a
`ValueError` naming the offending variable at startup — it fails loudly rather
than failing every send at runtime. A **missing** configuration is not an error
and keeps the Stage-1 default.

## Running the worker

As its own process (same idiom as `finalisma_cloud.service` / `.web`):

```bash
FINALISMA_SMTP_HOST=smtp.example.com FINALISMA_SMTP_PORT=587 \
FINALISMA_SMTP_USERNAME=apikey FINALISMA_SMTP_PASSWORD=secret \
FINALISMA_SMTP_FROM=no-reply@example.com \
FINALISMA_DB_PATH=./data/finalisma-cloud.db \
PYTHONPATH=src python -B -m finalisma_cloud.identity.outbox_worker
```

Run one pass and exit (handy for cron/systemd or manual verification):

```bash
PYTHONPATH=src python -B -m finalisma_cloud.identity.outbox_worker --once
```

Without SMTP configured the worker prints a note and exits `0`; every row stays
`queued`.

## Delivery contract

- **At-least-once with idempotency.** A row is claimed with a conditional
  `UPDATE … WHERE status = 'queued'` inside one `BEGIN IMMEDIATE` transaction.
  A row already claimed by a concurrent worker or an earlier pass is not
  returned, so it is never sent twice. `sent` and `failed` are terminal. A row
  stranded in `claimed` by a worker that died mid-send is reclaimed after a
  60s lease — that is the at-least-once window.
- **Retry with backoff.** Transient failures (`SMTPServerDisconnected`,
  connection/OS errors, 5xx-timeout responses) return the row to `queued` with
  `attempts` incremented and `next_attempt_at = now + backoff × attempts`.
  After `max_attempts` the row reaches the terminal `failed` state. Permanent
  failures (refused recipient / rejected sender) go straight to `failed`, so
  one bad address cannot block the queue forever.
- **No secrets in logs.** The worker logs only the row id, attempt count, and
  outcome classification. The email body and any reset/verification token it
  contains are never logged and never written to `last_error`.

## Verifying delivery

1. Seed a row the way a real user would — request a password reset:

   ```bash
   PYTHONPATH=src python -B - <<'PY'
   from finalisma_cloud.identity import accounts
   from finalisma_cloud.storage import SqliteWalBackend
   from finalisma_cloud.migrations import apply_migrations
   b = SqliteWalBackend("./data/finalisma-cloud.db"); b.initialize(); apply_migrations(b)
   accounts.request_password_reset(b, "tenant_<id>", "you@example.com")
   b.close()
   PY
   ```

2. Confirm the row is `queued`, then run the worker once:

   ```bash
   sqlite3 data/finalisma-cloud.db \
     "SELECT entry_id, to_email, status, attempts FROM cloud_identity_outbox"
   PYTHONPATH=src python -B -m finalisma_cloud.identity.outbox_worker --once
   sqlite3 data/finalisma-cloud.db \
     "SELECT entry_id, to_email, status, attempts, dispatched_at FROM cloud_identity_outbox"
   ```

   The row flips from `queued` to `sent` with a non-null `dispatched_at`, and
   the recipient's inbox receives the message. A permanent failure leaves the
   row `failed`; a transient one returns it to `queued` with a higher
   `attempts` count and a future `next_attempt_at`.

## Monitoring

Every pass logs one line per affected row (id + outcome only) and one summary
line per pass. `grep 'outbox'` on the worker's stderr/stdout shows the queue's
behaviour without ever exposing message content.

Authoritative spec: `docs/IDENTITY_DESIGN.md` §5 (the `Mailer` interface and
the Stage-1/Stage-2 split the design documents).
