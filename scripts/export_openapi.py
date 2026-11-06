#!/usr/bin/env python3
"""Export the committed OpenAPI schema (plan 10).

Usage::

    python3 scripts/export_openapi.py

The schema is generated from the app factory without a live container, which
is exactly the mode used for import-time tooling.  CI can regenerate the file
and fail if it differs from ``docs/openapi.json``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from devsandbox.api.app import create_app  # noqa: E402


def main() -> int:
    schema = create_app().openapi()
    destination = ROOT / "docs" / "openapi.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {destination} ({len(schema['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
