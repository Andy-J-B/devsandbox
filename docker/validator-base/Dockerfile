# syntax=docker/dockerfile:1
# Validator base image for DevSandbox validation commands (plan 02, extended by plan 09).
#
# Provides a minimal toolchain (python, node, git, build-essential) for running
# project validation commands inside a sandbox.  Plan 09 may extend this image
# with project-specific toolchains.

FROM python:3.12-slim AS base

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

# Non-root user matching the sandbox runtime (uid 1000).
RUN useradd -m -u 1000 agent

WORKDIR /workspace
USER agent

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/bin/sh"]
