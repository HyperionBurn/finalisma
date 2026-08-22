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
Signup verification messages contain a clickable `/verify?token=fvt_...` URL,
and organization-invite messages contain a clickable `/invite/fiv_...` URL.
Both origins come from `WEFT_WEB_PUBLIC_ORIGIN` when set, then
`WEFT_PUBLIC_ORIGIN`; authenticated APIs never return raw verification or
invite tokens.

## How it fits together

1. **Callers are unchanged.** `accounts.signup`, `accounts.request_password_reset`
   and `invites.create` keep writing a durable `cloud_identity_outbox` row via
   `LocalOutboxMailer` (Stage 1). A signup is never lost because mail failed.
2. **The worker drains the outbox.** `OutboxDrainer`
   (`src/weft_cloud/identity/outbox_worker.py`) claims undelivered rows
   and sends each through `SmtpMailer` (STARTTLS + AUTH, standard library
   only), marking every row `sent` or `failed`.
3. **Selection is by environment.** `mailer.build_mailer(backend)` returns
   `SmtpMailer` when `WEFT_SMTP_HOST` is set and `LocalOutboxMailer`
   otherwise. The worker only ever drains with an SMTP mailer — it can never
   re-enqueue a row into the outbox it is draining.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `WEFT_SMTP_HOST` | — (SMTP disabled) | SMTP relay host. Setting this enables delivery. |
| `WEFT_SMTP_PORT` | `587` | Submission port (STARTTLS). |
| `WEFT_SMTP_USERNAME` | — | Auth username (required once the host is set). |
| `WEFT_SMTP_PASSWORD` | — | Auth password (required once the host is set). |
| `WEFT_SMTP_FROM` | — | From address, e.g. `no-reply@example.com` (required once the host is set). |
| `WEFT_WEB_PUBLIC_ORIGIN` | `http://127.0.0.1:18789` | Origin used for clickable human invite acceptance URLs. Set it to the public nginx origin in a hosted deployment. |
| `WEFT_DB_PATH` | `./data/weft-cloud.db` | The **same** SQLite file the web/service processes write to, so the worker drains the file the service writes to — never an empty one. |
| `WEFT_DRAIN_INTERVAL` | `5` | Seconds between passes. |
| `WEFT_DRAIN_BATCH` | `50` | Max rows claimed per pass. |
| `WEFT_DRAIN_MAX_ATTEMPTS` | `5` | Retries before a transient failure becomes terminal. |
| `WEFT_DRAIN_BACKOFF` | `60` | Base seconds between retries (grows linearly per attempt). |

Validation follows the service launchers: a half-set SMTP configuration
(host without username/password/from, or a malformed port/interval) raises a
`ValueError` naming the offending variable at startup — it fails loudly rather
than failing every send at runtime. A **missing** configuration is not an error
and keeps the Stage-1 default.

## Upgrading an existing database

Migrations never run on a fresh database only. `ensure_schema` (and therefore
`apply_migrations`) is invoked on every boot and always brings the database up
to the latest migration — including the `cloud_008` delivery columns
(`status`, `attempts`, `next_attempt_at`, `claimed_at`, `claimed_by`,
`last_error`) that an outbox written before this feature existed is missing.
`cloud_008` is now a set of individually-guarded `ALTER TABLE` statements, so a
column that already exists is skipped rather than wedging the database forever.
The later `cloud_017_identity_email_canonical` migration trims and case-folds
historical account, invite, and outbox addresses before it adds the global
account-email uniqueness index. If two historical accounts collide after
canonicalization, the migration fails and rolls back without merging them.

To upgrade the live database in place (no data loss — the migration is
forward-only and additive):

```bash
PYTHONPATH=src python -B - <<'PY'
from weft_cloud.storage import SqliteWalBackend
from weft_cloud.migrations import apply_migrations
b = SqliteWalBackend("./data/weft-cloud.db")
apply_migrations(b)
b.close()
PY
```

Back up the database file first; the documented rollback is the pre-upgrade
backup (see `docs/DEPLOY.md` §4).

## Running the worker

As its own process (same idiom as `weft_cloud.service` / `.web`):

```bash
WEFT_SMTP_HOST=smtp.example.com WEFT_SMTP_PORT=587 \
WEFT_SMTP_USERNAME=apikey WEFT_SMTP_PASSWORD=secret \
WEFT_SMTP_FROM=no-reply@example.com \
WEFT_DB_PATH=./data/weft-cloud.db \
PYTHONPATH=src python -B -m weft_cloud.identity.outbox_worker
```

Run one pass and exit (handy for cron/systemd or manual verification):

```bash
PYTHONPATH=src python -B -m weft_cloud.identity.outbox_worker --once
```

Without SMTP configured the worker prints a note and exits `0`; every row stays
`queued`.

The Docker Compose stack exposes the same worker as an opt-in `delivery`
profile. After setting the SMTP variables above, run:

```bash
docker compose --profile delivery up -d --build
```

For the hosted VM, install `scripts/systemd/weft-outbox.service` and place
only the `WEFT_SMTP_*` values in `/etc/weft/weft-email.env` with mode 600. The
unit uses the canonical `/var/lib/finalisma/cloud.db` path and is included by
the deploy restart proof when present; installation, enablement, and a real
mailbox smoke test remain operator-controlled.

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
   from weft_cloud.identity import accounts
   from weft_cloud.storage import SqliteWalBackend
   from weft_cloud.migrations import apply_migrations
   b = SqliteWalBackend("./data/weft-cloud.db"); b.initialize(); apply_migrations(b)
   accounts.request_password_reset(b, "tenant_<id>", "you@example.com")
   b.close()
   PY
   ```

2. Confirm the row is `queued`, then run the worker once:

   ```bash
   sqlite3 data/weft-cloud.db \
     "SELECT entry_id, to_email, status, attempts FROM cloud_identity_outbox"
   PYTHONPATH=src python -B -m weft_cloud.identity.outbox_worker --once
   sqlite3 data/weft-cloud.db \
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
