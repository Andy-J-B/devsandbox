# syntax=docker/dockerfile:1
#
# DevSandbox agent base image (plan 04).
#
# Contains the OpenCode CLI pinned to a specific version, git, Python, Node,
# build-essential, and ripgrep.  It intentionally does NOT contain a Docker
# CLI/socket, sudo, SSH keys, or cloud credentials.  The sandbox manager runs
# it as uid 1000 with a read-only root filesystem and a single writable mount
# at /workspace.

ARG OPENCODE_VERSION=1.19.5
# Base image pinned by digest (node:22-bookworm-slim).
FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee

ARG OPENCODE_VERSION
ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/agent \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    OPENCODE_DISABLE_AUTOUPDATE=true \
    OPENCODE_DISABLE_PRUNE=true \
    OPENCODE_DISABLE_TERMINAL_TITLE=true \
    OPENCODE_DISABLE_CLAUDE_CODE=true \
    OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=true \
    OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=true \
    OPENCODE_DISABLE_LSP_DOWNLOAD=true \
    OPENCODE_DISABLE_PROJECT_CONFIG=true \
    OPENCODE_DISABLE_EXTERNAL_SKILLS=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Pin OpenCode; the build fails if the pinned version cannot be installed or
# does not report the expected version.
RUN npm install -g --no-fund --no-audit "opencode-ai@${OPENCODE_VERSION}" \
    && (test "$(opencode --version)" = "${OPENCODE_VERSION}" \
        || test "$(opencode --version)" = "v${OPENCODE_VERSION}") \
    && npm cache clean --force

# Non-root agent user (uid 1000) with a writable home and workspace.  Recent
# node:* images already ship a uid/gid 1000 account ("node"), so rename it
# rather than failing on a duplicate id.
RUN set -eux; \
    if getent passwd 1000 >/dev/null; then \
        current_user="$(getent passwd 1000 | cut -d: -f1)"; \
        current_group="$(getent group 1000 | cut -d: -f1)"; \
        usermod --login agent --home /home/agent "$current_user"; \
        if [ -n "$current_group" ] && [ "$current_group" != agent ]; then \
            groupmod --new-name agent "$current_group"; \
        fi; \
    else \
        useradd --uid 1000 --create-home --shell /bin/bash agent; \
    fi; \
    mkdir -p /workspace /opt/devsandbox /home/agent/.local/share/opencode; \
    chown -R agent:agent /workspace /home/agent

# Baseline config lives outside $HOME so it is not masked by the sandbox
# manager's tmpfs mount.  The adapter adds per-attempt overrides through
# OPENCODE_CONFIG_CONTENT, which has higher precedence.
COPY docker/agent-base/opencode.json /opt/devsandbox/opencode.json
ENV OPENCODE_CONFIG=/opt/devsandbox/opencode.json

USER agent
WORKDIR /workspace

# The sandbox manager supplies the real command; this keeps `docker run`
# usable for smoke tests.
CMD ["sleep", "infinity"]
