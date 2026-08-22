# Deploying Weft Cloud

Two processes make up the hosted SaaS surface, and both share ONE SQLite
database file:

- `src/weft_cloud/service.py` — the agent-facing API: accounts, orgs,
  sessions, agent keys, and multi-agent rooms over `/v1/*`, the hosted MCP
  endpoint at `POST /mcp` (authenticated, tenant-confined — see
  `docs/HOSTED_MCP_DESIGN.md`), plus liveness/readiness and token-gated
  metrics endpoints.
- `src/weft_cloud/web` — the browser front-end (signup/login/rooms) via
  `python -m weft_cloud.web`.

The surface a deployment must serve is:

- **REST rooms API** — `POST /v1/rooms/{create,connect,join,leave,
  remove_member,close,send,receipts,poll,wait,ack,heartbeat,revoke_link,
  event_log,groups}` plus `GET /v1/rooms` and `GET /v1/rooms/info`
  (`src/weft_cloud/service.py` route table). `receipts` and `remove_member`
  are the newest pair (`1e0aa5a`). Malformed cursors are a caller error:
  non-integer / negative / beyond-head `after_seq` and negative `seq` return
  **400** `invalid_argument` or `invalid_cursor` — never a 500
  (`src/weft_cloud/rooms.py`).
- **Hosted MCP** — `POST /mcp` exposes exactly **14 room tools**
  (`room_create` `room_list` `room_join` `room_send` `room_receipts` `room_poll`
  `room_wait` `room_info` `room_ack` `room_heartbeat` `room_leave`
  `room_remove_member` `room_close` `room_event_log`), pinned by
  `test_hosted_surface_is_a_small_correct_set`. Identity is never an
  argument: `agent_id` resolves from the authenticated `fss_` session or
  `agk_` agent key, and client-supplied identity fields are rejected.
- **SDK** — `src/weft_sdk/client.py` drives all 14 hosted tools; in hosted
  mode it strips identity arguments and surfaces HTTP 429 as structured
  `rate_limited` errors with `retry_after` (`c9f4e0e`).
- **Identity** — `POST /v1/auth/signout` with an `agk_` bearer truthfully
  revokes the key itself (and frees its room seats); org member-add can
  never mint an `owner` from an admin caller (`d344ebe`).

This runbook gets both processes into containers with durable state.
Commands are meant to be executed verbatim from the repository root.

## Read this first: the single-instance constraint

State is SQLite in WAL mode. SQLite-WAL supports exactly **one writer** and
many concurrent readers — *across processes*. That means the constraint now
spans **both** services: `weft-cloud` and `weft-web` both open the
same `/data/weft-cloud.db` file, so they must run as **one writer pair on
one persistent disk**. Never scale either of them horizontally, never place
either behind a load balancer, and never point the two services at different
disks — two processes on different machines sharing one SQLite file is unsafe
(WAL requires the `-wal`/`-shm` sidecars on a real local filesystem). One
machine, one disk, exactly one instance of each process. Making this a real
multi-node service is a storage-layer change (Postgres) and is out of scope
here — do not pretend otherwise.

Both services run schema bootstrap at startup (`initialize()` +
`ensure_schema`). This is safe to do concurrently: migration check-and-apply
runs inside a `BEGIN IMMEDIATE` write transaction, so the second process sees
the migrations already applied instead of racing them.

## Requirements

- Docker with Compose v2 (`docker compose version`).
- Python 3.11+ only if you run the service outside the container.

## 1. Build and run

```bash
docker compose up -d --build
```

Wait for the API and web services to become healthy:

```bash
docker compose ps
# NAME                 IMAGE                COMMAND              SERVICE           STATUS
# weft-cloud-1    weft-cloud:local python -m weft… weft-cloud   Up 3 seconds (healthy)
# weft-web-1      weft-cloud:local python -m weft… weft-web     Up 3 seconds (healthy)
```

The API and web containers come from the same image (`weft-cloud:local`). Both
mount the same named volume and open the same database file.

The hosted delivery worker is opt-in because its bundled sink is a local
journal, not a remote provider. Enable it only when local processing evidence
is useful:

```bash
docker compose --profile cloud-delivery up -d --build
docker compose logs -f weft-delivery
```

It consumes `cloud_outbox` rows and appends each processed envelope to
`/data/delivery.jsonl`. This is not proof that a remote provider delivered the
envelope. The worker's retry and lease state remains durable in the shared
database.

Email delivery is deliberately opt-in. The default stack queues verification,
reset, and organization-invite messages without sending them. To enable the
bundled SMTP worker, provide the SMTP settings through the shell or an ignored
local `.env`, then start the delivery profile:

```bash
export WEFT_SMTP_HOST=smtp.example.com
export WEFT_SMTP_PORT=587
export WEFT_SMTP_USERNAME=apikey
export WEFT_SMTP_PASSWORD='use-your-secret-store'
export WEFT_SMTP_FROM=no-reply@example.com
export WEFT_WEB_PUBLIC_ORIGIN=http://127.0.0.1:18789
docker compose --profile delivery up -d --build
```

The `weft-outbox` container uses the same `/data/weft-cloud.db` volume and
only the delivery profile starts it. Without SMTP configuration it exits
without sending; no credential is committed in this repository. Invite mail
contains a clickable `/invite/fiv_...` URL, while the authenticated REST
response continues to return metadata only—not the bearer token.

## 2. Verify

Health probe from the host for the agent API:

```bash
curl -fsS http://127.0.0.1:18788/healthz
# {"status":"ok","service":"weft-cloud"}
curl -fsS http://127.0.0.1:18788/readyz
# {"status":"ready","service":"weft-cloud"}
```

`/healthz` proves only that the process answers. `/readyz` runs a storage
transaction and returns 503 when the service cannot read its database. The
versioned `/v1/healthz` and `/v1/readyz` aliases are also available. The cloud
service exposes `/metrics` and `/v1/metrics` only when `WEFT_METRICS_TOKEN` is
non-empty; operators must send that value as a Bearer token. The response is
process-local Prometheus text with status labels only, not a tenant data API or
a cross-instance aggregate.

The browser front-end is published on the loopback only, at
`127.0.0.1:18789`:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18789/signup   # 200
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18789/login    # 200
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18789/rooms    # 303 → /login
```

The web container has no `/healthz`; its healthcheck probes `GET /login`
(200 without a session) instead.

Acceptance proof — this drives a real signup → one room → 4 agents → ordered
broadcast → unicast → refusal flow **against the containerised agent API** (it
already targets `127.0.0.1:18788`, which compose publishes):

```bash
PYTHONPATH=src python -B scripts/prove-multiagent.py
```

It must finish with `PROOF COMPLETE — ALL CLAIMS VERIFIED` and exit 0.

## 3. Configuration

Everything is configured by the environment; nothing is baked into the image
and the image contains no secrets.

| Variable | Default | Meaning |
|---|---|---|
| `WEFT_HOST` | `127.0.0.1` | Bind address. `0.0.0.0` inside a container. |
| `WEFT_PORT` | `18788` | HTTP port. |
| `WEFT_DB_PATH` | `./data/weft-cloud.db` | SQLite path. In the container this is the `/data` mount point. |
| `WEFT_PUBLIC_ORIGIN` | `http://127.0.0.1:18788` | Origin used for agent-facing room join links. Set this to the public nginx origin in a hosted deployment. |
| `WEFT_DELIVERY_SINK` | `/data/delivery.jsonl` | Sink path for direct worker runs. Compose pins the opt-in worker to the shared-volume path. A journal entry is local processing evidence, not remote-provider delivery proof. |
| `WEFT_DELIVERY_DRAIN_INTERVAL` | `5` | Seconds between hosted delivery passes. |
| `WEFT_DELIVERY_DRAIN_BATCH` | `50` | Maximum rows claimed per hosted delivery pass. |

The web front-end is configured the same way, with `WEFT_WEB_*` variables:

| Variable | Default | Meaning |
|---|---|---|
| `WEFT_WEB_HOST` | `127.0.0.1` | Bind address. `0.0.0.0` inside a container. |
| `WEFT_WEB_PORT` | `18789` | HTTP port. |
| `WEFT_WEB_DB_PATH` | `./data/weft-web.db` | **Must equal the agent API's `WEFT_DB_PATH`** — both processes share one store. In the container: `/data/weft-cloud.db`. |
| `WEFT_WEB_STATE_DIR` | `./data` | Scratch/state directory (unused by the current web code, kept for parity). |
| `WEFT_WEB_PUBLIC_ORIGIN` | `http://127.0.0.1:18789` | Origin used for human invite acceptance URLs. In a shared nginx deployment, set it to the same public origin as `WEFT_PUBLIC_ORIGIN`. |
| `WEFT_WEB_STATIC_DIR` | `./site` | Built marketing site, if any. The image does **not** carry the site, so compose leaves this unset; the web container serves no static files and unmatched GETs return 404. |

Precedence is argv > env > default, so the legacy launch form
(`python -B src/weft_cloud/service.py <port> <db-path>`) keeps working
unchanged. A malformed or out-of-range port fails at startup rather than
silently binding a default.

Run the agent API without a container:

```bash
WEFT_HOST=0.0.0.0 WEFT_PORT=18788 WEFT_DB_PATH=./data/cloud.db \
  PYTHONPATH=src python -B -m weft_cloud.service
```

Run the web front-end without a container (against the same database file so
the two processes share state):

```bash
WEFT_WEB_HOST=127.0.0.1 WEFT_WEB_PORT=18789 \
  WEFT_WEB_DB_PATH=./data/cloud.db WEFT_WEB_STATE_DIR=./data \
  PYTHONPATH=src python -B -m weft_cloud.web
```

The API and web running like this are the single-writer pair described above:
they must stay on one machine, and `./data/cloud.db` is the one shared file.
If you also run the opt-in delivery worker, it must use the same file. In local
development you may point `WEFT_WEB_STATIC_DIR` at `./site` to serve the
built marketing pages from the web app.

### Secret hygiene

There are no required secrets at startup today (auth is self-contained). When
SMTP, billing, or OIDC credentials arrive, inject them via the platform's
secrets manager (`fly secrets set`, Docker secrets, etc.) — never via compose,
`ENV`, or a committed `.env`. This repo never ships a secret in an image.

## 4. Back up the volume

The database lives in the named volume `weft-cloud-data`, shared by the API,
web, and optional delivery worker. Back up with all running services stopped
so the WAL is fully
checkpointed:

```bash
docker compose stop
docker run --rm -v weft-cloud-data:/data -v "$PWD":/backup \
  alpine tar czf /backup/weft-cloud-data-$(date +%F).tgz -C /data .
docker compose start
```

Restore (replaces current data):

```bash
docker compose stop
docker run --rm -v weft-cloud-data:/data -v "$PWD":/backup \
  alpine sh -c "rm -rf /data/* && tar xzf /backup/weft-cloud-data-YYYY-MM-DD.tgz -C /data"
docker compose start
```

Logs:

```bash
docker compose logs -f weft-cloud
docker compose logs -f weft-web
# Only when the optional profile is enabled:
docker compose logs -f weft-delivery
```

## 5. Roll back

There is no registry here, so rollback means rebuilding from a known-good tree
state. The named volume keeps your data across the rebuild:

```bash
git stash
git checkout <known-good-commit>
docker compose up -d --build
git checkout <your-branch> && git stash pop   # when you want your work back
```

`docker compose down` (no `-v`) preserves the volume. `docker compose down -v`
**deletes the database** — only use it to throw state away.

## 6. Hosting recommendation

Ship this as **one VM with one persistent disk** running **both** processes
against that one disk. Because the store is a single SQLite file shared by two
processes, the platform must give you exactly one instance of each, co-located
on durable filesystem storage:

- **Plain VPS** (DigitalOcean, Hetzner, …) running `docker compose up -d` is
  the cleanest fit: compose starts both containers on the same machine, sharing
  one volume. Cheapest, and the two-process single-writer pair is explicit.
- **Fly.io.** A single-machine app whose `fly.toml` pins
  `min_machines_running = 1` and `max_machines_running = 1`, plus a persistent
  volume mounted at `/data` (`fly volumes create`). Because there are now two
  processes, run both in that one machine (e.g. both `[processes]` in one app,
  or one app per process only if both machines share the same volume — verify
  that with your provider; the two must never land on different disks). Never
  enable autoscaling or a second machine — that violates the single-writer
  constraint.
- **Render.** A single service with a mounted disk is the same shape and a fine
  alternative if you already use it; put both processes in that one service.

Avoid serverless/shared-filesystem platforms and autoscaling groups: they
assume stateless horizontally-scaled workers, which this storage cannot do, and
a network filesystem breaks WAL's locking guarantees.

Deciding on a provider and signing up is the founder's call — nothing in this
repo creates accounts or pushes images anywhere.

## Verification status of this runbook

Executed and confirmed on the authoring machine:

- `PYTHONPATH=src python -B -m weft_cloud.service` with env config →
  `{"status":"ok","service":"weft-cloud"}`.
- `PYTHONPATH=src python -B scripts/prove-multiagent.py` against that service →
  exit 0, `PROOF COMPLETE`.
- Legacy argv form (`service.py <port> <db-path>`) → healthy, and argv won over
  a conflicting `WEFT_PORT` env var.
- `PYTHONPATH=src python -B -m weft_cloud.web` with env config → prints
  `weft-web listening on http://127.0.0.1:<port>` and serves `GET /signup`
  → 200, `GET /login` → 200; a non-integer `WEFT_WEB_PORT` exits 2 with
  `weft-web: WEFT_WEB_PORT must be an integer`.
- Both processes started against one shared database file; the shared-store
  suite `tests/test_webapp_entrypoint.py` (6 tests) passes.
- The latest local regression is recorded in
  [`RELEASE_EVIDENCE.md`](RELEASE_EVIDENCE.md): 1278 tests discovered, 1277
  passed, and 1 skipped (measured 2026-08-22 on the hosted room lifecycle
  stack). This is not Docker, VM, hosted-edge, SMTP, or merge proof; the
  historical 915-test and 524-test snapshots above remain provenance for
  earlier deployment runs.

Performance numbers are deliberately absent from this runbook. The locked
performance gate is currently **red under host-load noise** (recent captures
ran while multiple agent sessions were active on the host); its provenance,
rules, and the requirement of a controlled idle-host rerun live in
`docs/PERFORMANCE.md`. Do not publish a deployment performance figure that
does not trace to that file.

Not executed on the authoring machine (no Docker runtime installed):
`docker build`, `docker compose up`, the volume backup/restore and the
`/healthz`-from-outside-container check. Those commands are written to the
documented contract, `compose.yaml` was validated by parsing it as YAML (both
services resolve, web `command`/port/volume/healthcheck as intended), and the
container entry commands above were reviewed, but the image build itself
remains untested until it runs where Docker exists. These are deployment
verification gaps, not evidence that the container is ready for production.
