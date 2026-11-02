# br0kr runtime image. Built + smoke-tested in CI but NEVER published (the broker
# is pre-alpha). It ships two real entrypoints (see the CMD note below); the default
# CMD stays an import smoke so CI's `docker run --rm` needs no DB.
# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The broker: the importable package, the schema, and the runnable proofs.
COPY br0kr/ ./br0kr/
COPY db/ ./db/
COPY scripts/ ./scripts/

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 broker && chown -R broker:broker /app
USER broker

# Entrypoints (PGCONN + per-cluster token secrets in the env, never in the image). Run migrations
# ONCE first, then the long-running services:
#   docker run -e PGCONN=... br0kr python -m br0kr.migrate         (apply schema migrations)
#   docker run -e PGCONN=... br0kr python -m br0kr.mcp_server      (agent-facing MCP gateway)
#   docker run -e PGCONN=... br0kr python -m br0kr.daemon          (reconciler service)
# Run the daemon as a SINGLE leader (a second instance is a hot standby); run as many gateways as
# you like. The whole stack — incl. an HA Postgres ledger — is wired in docker-compose.yml
# (`docker compose up -d`); see docs/runbooks/docker-deploy.md.
#
# The default CMD stays an import smoke (no DB needed for `docker run --rm`): it imports the gateway
# graph, the daemon, the migration runner, the scheduler/PDM drivers, and the cross-cluster
# lease-migration saga, so a broken import or a missing runtime dep fails the CI smoke test immediately.
CMD ["python", "-c", "import br0kr, br0kr.mcp_server, br0kr.daemon, br0kr.migrate, br0kr.scheduler, br0kr.pdm, br0kr.migration, br0kr.observability; print('br0kr', br0kr.__version__, 'image OK')"]
