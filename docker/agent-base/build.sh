#!/usr/bin/env bash
# Build the DevSandbox agent base image and smoke-test the pinned OpenCode CLI.
set -euo pipefail

cd "$(dirname "$0")/../.."

OPENCODE_VERSION="${OPENCODE_VERSION:-1.19.5}"
IMAGE="${IMAGE:-devsandbox/agent:latest}"

echo "==> building ${IMAGE} (opencode ${OPENCODE_VERSION})"
docker build \
  --build-arg "OPENCODE_VERSION=${OPENCODE_VERSION}" \
  -f docker/agent-base/Dockerfile \
  -t "${IMAGE}" \
  .

echo "==> smoke test: opencode --version"
docker run --rm --network none --entrypoint opencode "${IMAGE}" --version

echo "==> smoke test: no docker/sudo/ssh in image"
"$(dirname "$0")/smoke.sh" "${IMAGE}"
