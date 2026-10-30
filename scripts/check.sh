#!/usr/bin/env bash
set -euo pipefail

echo "==> ruff check"
python -m ruff check .

echo "==> ruff format --check"
python -m ruff format --check .

echo "==> mypy (strict)"
python -m mypy

echo "==> pytest"
python -m pytest -q
