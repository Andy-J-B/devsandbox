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

# Node 22 from the official image, pinned by digest.  Debian's nodejs/npm
# packages pull an old bundled handlebars tree with fixable critical CVEs, so
# copy the upstream Node runtime instead of installing the distro packages.
FROM node:22-trixie-slim@sha256:68c76cfad89921b927eae28ba21407475e29d0ce450afcdbe70c1532db042b64 AS node

# Base image pinned by digest (python:3.12-slim).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Upgrade first so the image picks up Debian security updates for packages
# that were current when the base digest was published.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        procps \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version \
    && npm --version

# Pre-baked Python validation tools, pinned to exact versions so the image is
# reproducible.  Update these deliberately (see docs/images.md).
RUN pip install --no-cache-dir \
        pytest==9.2.0 \
        pytest-asyncio==1.4.1.post1 \
        ruff==0.17.4

# Non-root user matching the sandbox runtime (uid 1000).
RUN useradd -m -u 1000 agent

WORKDIR /workspace
USER agent

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/bin/sh"]
