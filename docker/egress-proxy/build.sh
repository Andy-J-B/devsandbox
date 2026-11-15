#!/usr/bin/env bash
# Build and smoke-test the DevSandbox egress proxy image.
set -euo pipefail

cd "$(dirname "$0")/../.."

IMAGE="${IMAGE:-devsandbox/egress-proxy:latest}"

echo "==> building ${IMAGE}"
docker build \
  -f docker/egress-proxy/Dockerfile \
  -t "${IMAGE}" \
  .

echo "==> smoke test: empty allowlist refuses to start"
if docker run --rm --network none -e DEVSANDBOX_ALLOWED_HOSTS= "${IMAGE}" >/dev/null 2>&1; then
  echo "proxy started with an empty allowlist" >&2
  exit 1
fi

echo "==> smoke test: health endpoint"
name="ds-egress-smoke-$$"
cleanup() {
  docker rm -f "${name}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --rm \
  --name "${name}" \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -e DEVSANDBOX_ALLOWED_HOSTS=example.com:443 \
  "${IMAGE}" >/dev/null

for _ in $(seq 1 30); do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${name}")"
  if [ "${status}" = "healthy" ]; then
    echo "==> proxy healthy"
    exit 0
  fi
  if [ "${status}" = "unhealthy" ] || [ "${status}" = "none" ]; then
    echo "==> proxy failed to become healthy (status: ${status})" >&2
    docker logs "${name}" >&2 || true
    exit 1
  fi
  sleep 0.5
done

echo "==> proxy did not become healthy in time" >&2
docker logs "${name}" >&2 || true
exit 1
