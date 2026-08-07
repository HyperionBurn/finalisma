# syntax=docker/dockerfile:1
#
# Finalisma Cloud — the hosted SaaS surface.
#
# A deliberate small, boring image: the cloud service is stdlib-only with zero
# runtime dependencies, so there is no build step, no package install and no
# network access at runtime. The image carries only the source packages
# (finalisma_cloud, plus the stdlib-only finalisma_mcp / finalisma_sdk).
#
# SINGLE-INSTANCE BOUNDARY: state is SQLite in WAL mode, which supports exactly
# ONE writer. This image must be run as a single process bound to a persistent
# disk (/data). It must NOT be scaled behind a load balancer. See docs/DEPLOY.md.
#
# Configuration is via the environment only (FINALISMA_HOST, FINALISMA_PORT,
# FINALISMA_DB_PATH). No secret is ever baked into this image.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    FINALISMA_HOST=0.0.0.0 \
    FINALISMA_PORT=18788 \
    FINALISMA_DB_PATH=/data/finalisma-cloud.db

WORKDIR /app

# Run as an unprivileged user. The DB mount point /data is owned by that user
# so a named volume initialized from this image inherits writable ownership.
RUN groupadd --system finalisma \
    && useradd --system --gid finalisma --no-create-home finalisma \
    && mkdir -p /app/src /data \
    && chown -R finalisma:finalisma /app /data

# .dockerignore keeps tests, the marketing site (site/, web/), docs, .git and
# render artifacts out of the build context.
COPY --chown=finalisma:finalisma src/ /app/src/

USER finalisma

# The database mount point. Compose mounts a named volume here so state
# survives restarts; never bake the database into a filesystem layer.
VOLUME ["/data"]

EXPOSE 18788

# Stdlib-only health probe hitting /healthz (no curl/wget in the slim image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('FINALISMA_PORT','18788')+'/healthz', timeout=3)"

CMD ["python", "-B", "-m", "finalisma_cloud.service"]
