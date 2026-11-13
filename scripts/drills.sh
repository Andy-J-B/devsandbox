#!/usr/bin/env bash
# Opt-in operational drills: Docker outage, PostgreSQL restart, disk-full
# handling, and idle resource use.  See docs/drills.md.
#
# Usage:
#   scripts/drills.sh
#   scripts/drills.sh -k postgres
#   DEVSANDBOX_DRILL_IDLE_SECONDS=300 scripts/drills.sh
#
# Requirements:
#   * Docker daemon reachable by the current user
#   * devsandbox/validator:latest (built automatically when missing)
#   * PostgreSQL reachable via DEVSANDBOX_DATABASE_URL, or start compose with
#     DEVSANDBOX_E2E_START_POSTGRES=1
#
# The PostgreSQL restart drill starts its own throwaway container; it never
# restarts the database named by DEVSANDBOX_DATABASE_URL.
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

export DEVSANDBOX_DRILLS=1
export DEVSANDBOX_E2E=1
export DEVSANDBOX_RUN_VALIDATION_INTEGRATION=1
export DEVSANDBOX_DATABASE_URL="$DB_URL"
export DEVSANDBOX_VALIDATOR_IMAGE="${DEVSANDBOX_VALIDATOR_IMAGE:-devsandbox/validator:latest}"

echo "==> pytest -m drill"
python3 -m pytest -q -m drill "$@"
