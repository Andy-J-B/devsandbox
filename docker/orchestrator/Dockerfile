# syntax=docker/dockerfile:1
#
# DevSandbox orchestrator image (plan 10).
#
# Serves the FastAPI app and runs the orchestration engine.  The container does
# NOT contain a Docker CLI or socket: the orchestrator talks to the *host*
# daemon through the Docker SDK, and the compose file mounts the socket only
# into this service.  Never mount docker.sock into agent/validator containers.

# Base image pinned by digest (python:3.12-slim).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# PostgreSQL client tools for `devsandbox db backup/verify/restore`.  The major
# version must be equal to (or newer than) the database server because pg_dump
# refuses to dump a newer server.  Override at build time when your server is
# newer:
#   docker build --build-arg POSTGRES_CLIENT_MAJOR=18 ...
ARG POSTGRES_CLIENT_MAJOR=17

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        tini \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        "postgresql-client-${POSTGRES_CLIENT_MAJOR}" \
    && ln -sf "/usr/lib/postgresql/${POSTGRES_CLIENT_MAJOR}/bin/pg_dump" /usr/local/bin/pg_dump \
    && ln -sf "/usr/lib/postgresql/${POSTGRES_CLIENT_MAJOR}/bin/pg_restore" /usr/local/bin/pg_restore \
    && ln -sf "/usr/lib/postgresql/${POSTGRES_CLIENT_MAJOR}/bin/psql" /usr/local/bin/psql \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 devsandbox \
    && mkdir -p /data \
    && chown -R devsandbox:devsandbox /data

ENV DEVSANDBOX_DATA_ROOT=/data \
    DEVSANDBOX_API__HOST=0.0.0.0 \
    DEVSANDBOX_API__PORT=8000

USER devsandbox
EXPOSE 8000

# `devsandbox serve` applies packaged Alembic migrations before binding the
# API (serialized with a PostgreSQL advisory lock).  Disable with
# DEVSANDBOX_MIGRATIONS__AUTO_UPGRADE=false when migrations are a separate
# deployment step.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"]
STOPSIGNAL SIGTERM

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["devsandbox", "serve"]
