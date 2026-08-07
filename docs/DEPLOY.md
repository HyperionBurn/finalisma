# Deploying Finalisma Cloud

`src/finalisma_cloud/service.py` is the hosted SaaS surface: accounts, orgs,
sessions, and multi-agent rooms over one HTTP API (`/v1/*`, plus `/healthz`).
This runbook gets it into a container with durable state. Commands are meant to
be executed verbatim from the repository root.

## Read this first: the single-instance constraint

State is SQLite in WAL mode. SQLite-WAL supports exactly **one writer**, so
this deployment is **one instance** and must **never be scaled horizontally**
behind a load balancer. You run one container bound to one persistent disk;
durability comes from the volume mount, not from replicas. Do not autoscale it.
Making it a real multi-node service is a storage-layer change (Postgres) and is
out of scope here — do not pretend otherwise.

## Requirements

- Docker with Compose v2 (`docker compose version`).
- Python 3.11+ only if you run the service outside the container.

## 1. Build and run

```bash
docker compose up -d --build
```

Wait for it to become healthy:

```bash
docker compose ps
# NAME                IMAGE                 COMMAND   SERVICE           STATUS
# finalisma-cloud-1   finalisma-cloud:local ...       finalisma-cloud   Up 3 seconds (healthy)
```

## 2. Verify

Health probe from the host:

```bash
curl -fsS http://127.0.0.1:18788/healthz
# {"status":"ok","service":"finalisma-cloud"}
```

Acceptance proof — this drives a real signup → one room → 4 agents → ordered
broadcast → unicast → refusal flow **against the containerised service** (it
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
| `FINALISMA_HOST` | `127.0.0.1` | Bind address. `0.0.0.0` inside a container. |
| `FINALISMA_PORT` | `18788` | HTTP port. |
| `FINALISMA_DB_PATH` | `./data/finalisma-cloud.db` | SQLite path. In the container this is the `/data` mount point. |

Precedence is argv > env > default, so the legacy launch form
(`python -B src/finalisma_cloud/service.py <port> <db-path>`) keeps working
unchanged. A malformed or out-of-range port fails at startup rather than
silently binding a default.

Run it without a container:

```bash
FINALISMA_HOST=0.0.0.0 FINALISMA_PORT=18788 FINALISMA_DB_PATH=./data/cloud.db \
  PYTHONPATH=src python -B -m finalisma_cloud.service
```

### Secret hygiene

There are no required secrets at startup today (auth is self-contained). When
SMTP, billing, or OIDC credentials arrive, inject them via the platform's
secrets manager (`fly secrets set`, Docker secrets, etc.) — never via compose,
`ENV`, or a committed `.env`. This repo never ships a secret in an image.

## 4. Back up the volume

The database lives in the named volume `finalisma-cloud-data`. Back up with the
service stopped so the WAL is fully checkpointed:

```bash
docker compose stop
docker run --rm -v finalisma-cloud-data:/data -v "$PWD":/backup \
  alpine tar czf /backup/finalisma-cloud-data-$(date +%F).tgz -C /data .
docker compose start
```

Restore (replaces current data):

```bash
docker compose stop
docker run --rm -v finalisma-cloud-data:/data -v "$PWD":/backup \
  alpine sh -c "rm -rf /data/* && tar xzf /backup/finalisma-cloud-data-YYYY-MM-DD.tgz -C /data"
docker compose start
```

Logs:

```bash
docker compose logs -f finalisma-cloud
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

Ship this as **one VM with one persistent disk**. Because the store is
single-writer, the platform must give you exactly one process and durable
filesystem storage:

- **Fly.io — recommended.** A single-machine app whose `fly.toml` pins
  `min_machines_running = 1` and `max_machines_running = 1`, plus a persistent
  volume mounted at `/data` (`fly volumes create`). It deploys this Dockerfile
  directly, performs health checks, and makes the single-instance boundary
  explicit. Never enable autoscaling or a second machine — that violates the
  single-writer constraint.
- **Render.** A single web service with a mounted disk is the same shape and a
  fine alternative if you already use it.
- **Plain VPS** (DigitalOcean, Hetzner, …) running `docker compose up -d` is
  the cheapest and gives identical guarantees, at the cost of managing the
  machine yourself.

Avoid serverless/shared-filesystem platforms and autoscaling groups: they
assume stateless horizontally-scaled workers, which this storage cannot do.

Deciding on a provider and signing up is the founder's call — nothing in this
repo creates accounts or pushes images anywhere.

## Verification status of this runbook

Executed and confirmed on the authoring machine:

- `PYTHONPATH=src python -B -m finalisma_cloud.service` with env config →
  `{"status":"ok","service":"finalisma-cloud"}`.
- `PYTHONPATH=src python -B scripts/prove-multiagent.py` against that service →
  exit 0, `PROOF COMPLETE`.
- Legacy argv form (`service.py <port> <db-path>`) → healthy, and argv won over
  a conflicting `FINALISMA_PORT` env var.
- 518 tests, 0 new failures (baseline pre-existing failures unchanged).

Not executed on the authoring machine (no Docker runtime installed):
`docker build`, `docker compose up`, the volume backup/restore and the
`/healthz`-from-outside-container check. Those commands are written to the
documented contract and the container entry command above was verified, but the
image build itself should be treated as untested until it runs where Docker
exists.
