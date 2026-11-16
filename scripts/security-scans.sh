#!/usr/bin/env bash
#
# Local dependency, secret, and static-analysis checks.
#
# Mirrors the `dependency-scan`, `secret-scan`, and `codeql` CI jobs. CodeQL
# needs the CodeQL CLI and a database build, so it only runs when the CLI is
# installed (or when CODEQL_REQUIRE=1 is set).
#
# Requirements:
#   pip-audit  -> pip install pip-audit
#   gitleaks   -> https://github.com/gitleaks/gitleaks#installing
#   codeql     -> https://github.com/github/codeql-cli-binaries (optional)
#
# Usage:
#   scripts/security-scans.sh
#   scripts/security-scans.sh --no-codeql
#   CODEQL_REQUIRE=1 scripts/security-scans.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_CODEQL=1
for argument in "$@"; do
  case "$argument" in
    --no-codeql) RUN_CODEQL=0 ;;
    *) echo "unknown argument: $argument" >&2; exit 2 ;;
  esac
done

cd "$ROOT"

echo "==> pip-audit"
if ! command -v pip-audit >/dev/null 2>&1; then
  echo "pip-audit is not installed (pip install pip-audit)" >&2
  exit 2
fi
pip-audit . --skip-editable --progress-spinner off --strict

echo
echo "==> gitleaks"
if ! command -v gitleaks >/dev/null 2>&1; then
  echo "gitleaks is not installed (see https://github.com/gitleaks/gitleaks)" >&2
  exit 2
fi
gitleaks detect --source "$ROOT" --config "$ROOT/.gitleaks.toml" \
  --no-banner --redact --exit-code 1

echo
echo "==> codeql"
if [[ "$RUN_CODEQL" == "0" ]]; then
  echo "skipped (--no-codeql)"
elif ! command -v codeql >/dev/null 2>&1; then
  if [[ "${CODEQL_REQUIRE:-0}" == "1" ]]; then
    echo "codeql is required but not installed" >&2
    exit 2
  fi
  echo "skipped (codeql CLI not installed)"
else
  DB="$ROOT/.codeql-db"
  RESULTS="$ROOT/dist/codeql-results.sarif"
  mkdir -p "$(dirname "$RESULTS")"
  rm -rf "$DB"
  codeql database create "$DB" \
    --language=python \
    --source-root="$ROOT" \
    --overwrite
  codeql database analyze "$DB" \
    --format=sarif-latest \
    --output="$RESULTS" \
    python-security-and-quality.qls
  echo "SARIF written to $RESULTS"
fi
