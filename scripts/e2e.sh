#!/usr/bin/env bash
# Real end-to-end acceptance suite: PostgreSQL + real Docker + real Git.
#
# Usage:
#   scripts/e2e.sh                     # all E2E scenarios
#   scripts/e2e.sh -k e5               # a subset
#
# Requirements:
#   * Docker daemon reachable by the current user
#   * devsandbox/validator:latest (build once with
#     `docker build -t devsandbox/validator:latest docker/validator-base`)
#
# By default an existing PostgreSQL on localhost:5432 is reused; set
# DEVSANDBOX_E2E_START_POSTGRES=1 to start the compose service when none is
# running.  The suite always uses isolated tables and drops them per test.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! docker info >/dev/null 2>&1; then
    echo "error: Docker daemon is not reachable" >&2
    exit 1
fi

if ! docker image inspect devsandbox/validator:latest >/dev/null 2>&1; then
    echo "==> building devsandbox/validator:latest"
    docker build -t devsandbox/validator:latest docker/validator-base
fi

DB_URL="${DEVSANDBOX_DATABASE_URL:-postgresql+asyncpg://devsandbox:devsandbox@localhost:5432/devsandbox}"

if [[ "${DEVSANDBOX_E2E_START_POSTGRES:-0}" == "1" ]]; then
    echo "==> starting compose postgres"
    docker compose -f docker/compose.yaml up -d postgres
    DB_URL="postgresql+asyncpg://devsandbox:devsandbox@localhost:5432/devsandbox"
fi

export DEVSANDBOX_E2E=1
export DEVSANDBOX_RUN_VALIDATION_INTEGRATION=1
export DEVSANDBOX_DATABASE_URL="$DB_URL"
export DEVSANDBOX_VALIDATOR_IMAGE="${DEVSANDBOX_VALIDATOR_IMAGE:-devsandbox/validator:latest}"

echo "==> pytest -m e2e"
python3 -m pytest -q -m e2e "$@"
