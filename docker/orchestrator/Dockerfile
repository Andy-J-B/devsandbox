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

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        tini \
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

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["devsandbox", "serve"]
