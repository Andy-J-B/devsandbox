#!/usr/bin/env bash
# Full local verification: fast gate, integration, security, E2E, and soak.
#
# Every stage runs even when an earlier one fails so a single invocation shows
# the complete picture.  The script exits non-zero if any stage fails and prints
# a summary with the per-stage result and duration.
#
# Usage:
#   scripts/check-all.sh
#   CHECK_ALL_FAIL_FAST=1 scripts/check-all.sh   # stop at the first failure
#   CHECK_ALL_DRY_RUN=1 scripts/check-all.sh     # print the plan, run nothing
#   DEVSANDBOX_SOAK_RUNS=10 scripts/check-all.sh # shorter soak
#
# Requirements:
#   * Docker daemon reachable by the current user
#   * devsandbox/validator:latest (built automatically when missing)
#   * PostgreSQL reachable via DEVSANDBOX_DATABASE_URL, or let testcontainers
#     provide one; set DEVSANDBOX_E2E_START_POSTGRES=1 to start compose
#
# The E2E stage excludes the randomized soak test; the soak stage runs exactly
# that test so one slow function cannot hide the rest of the E2E result.
set -uo pipefail

cd "$(dirname "$0")/.."

DRY_RUN="${CHECK_ALL_DRY_RUN:-0}"
FAIL_FAST="${CHECK_ALL_FAIL_FAST:-0}"
VALIDATOR_IMAGE="${DEVSANDBOX_VALIDATOR_IMAGE:-devsandbox/validator:latest}"
SOAK_TARGET="tests/e2e/test_e2e_crash_soak.py::test_soak_random_crash_points"

STAGE_NAMES=()
STAGE_STATUS=()
STAGE_SECONDS=()
FAILED_NAMES=()
STARTED_AT=$(date +%s)

if [[ "$DRY_RUN" != "1" ]]; then
    if ! command -v docker >/dev/null 2>&1; then
        echo "error: docker CLI not found; check-all needs a Docker daemon" >&2
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "error: Docker daemon is not reachable" >&2
        exit 1
    fi
    if ! docker image inspect "$VALIDATOR_IMAGE" >/dev/null 2>&1; then
        if [[ "$VALIDATOR_IMAGE" != "devsandbox/validator:latest" ]]; then
            echo "error: validator image '$VALIDATOR_IMAGE' is not present locally" >&2
            echo "       build or pull it, or unset DEVSANDBOX_VALIDATOR_IMAGE" >&2
            exit 1
        fi
        echo "==> building $VALIDATOR_IMAGE"
        docker build -t "$VALIDATOR_IMAGE" docker/validator-base || exit 1
    fi

    if [[ "${DEVSANDBOX_E2E_START_POSTGRES:-0}" == "1" ]]; then
        echo "==> starting compose postgres"
        docker compose -f docker/compose.yaml up -d postgres || exit 1
        export DEVSANDBOX_DATABASE_URL="postgresql+asyncpg://devsandbox:devsandbox@localhost:5432/devsandbox"
    fi
fi

export DEVSANDBOX_E2E=1
export DEVSANDBOX_RUN_VALIDATION_INTEGRATION=1
export DEVSANDBOX_VALIDATOR_IMAGE="$VALIDATOR_IMAGE"
export DEVSANDBOX_SOAK_RUNS="${DEVSANDBOX_SOAK_RUNS:-50}"

run_stage() {
    local name="$1"
    shift
    local started ended status

    printf '\n==> [%s] %s\n' "$name" "$*"
    if [[ "$DRY_RUN" == "1" ]]; then
        status=0
        ended=$STARTED_AT
        started=$STARTED_AT
    else
        started=$(date +%s)
        "$@"
        status=$?
        ended=$(date +%s)
    fi

    STAGE_NAMES+=("$name")
    STAGE_STATUS+=("$status")
    STAGE_SECONDS+=("$((ended - started))")
    if ((status != 0)); then
        FAILED_NAMES+=("$name")
        if [[ "$FAIL_FAST" == "1" ]]; then
            finish
            exit 1
        fi
    fi
}

finish() {
    local total=$(( $(date +%s) - STARTED_AT ))
    local i result

    printf '\n==================== check-all summary ====================\n'
    printf '%-12s %-6s %s\n' "STAGE" "RESULT" "TIME"
    for i in "${!STAGE_NAMES[@]}"; do
        result="PASS"
        if ((STAGE_STATUS[i] != 0)); then
            result="FAIL"
        fi
        printf '%-12s %-6s %ss\n' "${STAGE_NAMES[i]}" "$result" "${STAGE_SECONDS[i]}"
    done
    printf '%s\n' "-----------------------------------------------------------"
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "check-all: dry run; nothing was executed"
    elif ((${#FAILED_NAMES[@]} > 0)); then
        echo "check-all: ${#FAILED_NAMES[@]} stage(s) failed: ${FAILED_NAMES[*]}" >&2
    else
        echo "check-all: all stages passed"
    fi
    echo "total time: ${total}s"
}

run_stage check ./scripts/check.sh
run_stage integration python3 -m pytest -q -m "integration and not docker"
run_stage security python3 -m pytest -q -m security
run_stage e2e ./scripts/e2e.sh --deselect="$SOAK_TARGET"
run_stage soak ./scripts/e2e.sh "$SOAK_TARGET"

finish
if ((${#FAILED_NAMES[@]} > 0)); then
    exit 1
fi
