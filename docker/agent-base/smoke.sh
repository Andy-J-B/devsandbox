#!/usr/bin/env bash
# Smoke-test the agent image's tools and forbidden binaries.
set -euo pipefail

IMAGE="${1:-devsandbox/agent:latest}"

docker run --rm --network none --entrypoint opencode "${IMAGE}" --version

docker run --rm --network none --entrypoint sh "${IMAGE}" -c '
  set -eu
  git --version
  python3 --version
  node --version
  npm --version
  rg --version | head -n 1
  test "$(id -u)" = "1000"
  test "$(id -un)" = "agent"
  test "$(pwd)" = "/workspace"
  for forbidden in docker sudo ssh aws gcloud az; do
    if command -v "$forbidden" >/dev/null 2>&1; then
      echo "forbidden binary present: $forbidden" >&2
      exit 1
    fi
  done
  test ! -e /var/run/docker.sock
  test ! -d /home/agent/.ssh
  echo "agent image smoke test OK"
'
