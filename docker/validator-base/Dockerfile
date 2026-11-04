# syntax=docker/dockerfile:1
# Validator base image for DevSandbox validation commands (plan 02, extended by plan 09).
#
# Provides a minimal toolchain (python, node, git, build-essential) for running
# project validation commands inside a sandbox.  The Python test/lint tools are
# pre-installed so the common case needs no network at validation time; projects
# with other toolchains should use a project-specific validator image.
#
# Build with:
#   docker build -t devsandbox/validator:latest docker/validator-base

FROM python:3.12-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        nodejs \
        npm \
        procps \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Pre-baked Python validation tools (no dependency installation at check time).
RUN pip install --no-cache-dir \
        pytest \
        pytest-asyncio \
        ruff

# Non-root user matching the sandbox runtime (uid 1000).
RUN useradd -m -u 1000 agent

WORKDIR /workspace
USER agent

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/bin/sh"]
