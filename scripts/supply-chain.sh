#!/usr/bin/env bash
#
# Build the DevSandbox runtime images, generate SPDX SBOMs with Syft, and scan
# them with Grype.  This is the local equivalent of the `supply-chain` CI job
# in .github/workflows/security.yml.
#
# Requirements: docker, syft, grype (and cosign only for signing).
# Install hints:
#   https://github.com/anchore/syft#installation
#   https://github.com/anchore/grype#installation
#   https://docs.sigstore.dev/cosign/installation/
#
# Usage:
#   scripts/supply-chain.sh
#   SUPPLY_CHAIN_IMAGES="agent validator" scripts/supply-chain.sh
#   GRYPE_FAIL_ON=critical scripts/supply-chain.sh
#   SUPPLY_CHAIN_SKIP_BUILD=1 scripts/supply-chain.sh
#
# Signing is opt-in because it needs a registry reference (images must be
# pushed first) and either a key or GitHub OIDC:
#   COSIGN_IMAGE_REFS="ghcr.io/me/devsandbox-agent@sha256:..." \
#     scripts/supply-chain.sh
#   COSIGN_KEY=/path/to/cosign.key ...   # use a specific key
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${SUPPLY_CHAIN_OUT:-$ROOT/dist/supply-chain}"
PREFIX="${SUPPLY_CHAIN_IMAGE_PREFIX:-devsandbox}"
FAIL_ON="${GRYPE_FAIL_ON:-high}"
IMAGES="${SUPPLY_CHAIN_IMAGES:-agent validator egress-proxy}"

missing=()
for tool in docker syft grype; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if ((${#missing[@]} > 0)); then
  echo "missing required tools: ${missing[*]}" >&2
  echo "see the install hints at the top of scripts/supply-chain.sh" >&2
  exit 2
fi

mkdir -p "$OUT"

dockerfile_for() {
  case "$1" in
    agent) echo "docker/agent-base/Dockerfile" ;;
    validator) echo "docker/validator-base/Dockerfile" ;;
    egress-proxy) echo "docker/egress-proxy/Dockerfile" ;;
    *)
      echo "unknown image: $1" >&2
      return 1
      ;;
  esac
}

build_image() {
  local name="$1"
  local image="$PREFIX/$name:local"
  if [[ "${SUPPLY_CHAIN_SKIP_BUILD:-0}" == "1" ]]; then
    echo "==> using existing $image"
    return
  fi
  echo "==> building $image"
  docker build -f "$(dockerfile_for "$name")" -t "$image" "$ROOT"
}

for name in $IMAGES; do
  image="$PREFIX/$name:local"
  sbom="$OUT/$name.spdx.json"

  build_image "$name"

  echo "==> generating SBOM: $sbom"
  syft "$image" -o spdx-json >"$sbom"

  echo "==> scanning $name (fail on $FAIL_ON)"
  grype "sbom:$sbom" --fail-on "$FAIL_ON" --output table
done

if [[ -n "${COSIGN_IMAGE_REFS:-}" ]]; then
  if ! command -v cosign >/dev/null 2>&1; then
    echo "COSIGN_IMAGE_REFS is set but cosign is not installed" >&2
    exit 2
  fi
  for ref in $COSIGN_IMAGE_REFS; do
    echo "==> signing $ref"
    if [[ -n "${COSIGN_KEY:-}" ]]; then
      cosign sign --yes --key "$COSIGN_KEY" "$ref"
    else
      cosign sign --yes "$ref"
    fi
  done
fi

echo
echo "SBOMs and scan results are in $OUT"
