#!/usr/bin/env bash
set -euo pipefail

echo "==> ruff check"
python3 -m ruff check .

echo "==> ruff format --check"
python3 -m ruff format --check .

echo "==> mypy (strict)"
python3 -m mypy

echo "==> pytest"
python3 -m pytest -q
