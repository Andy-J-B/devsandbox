#!/usr/bin/env bash
# Fast local quality gate: lint, format, types, unit + contract tests.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> ruff check"
python3 -m ruff check .

echo "==> ruff format --check"
python3 -m ruff format --check .

echo "==> mypy (strict)"
python3 -m mypy

echo "==> pytest (unit + contract)"
python3 -m pytest -q -m "unit or contract"
