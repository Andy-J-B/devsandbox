"""The committed OpenAPI schema must match the app factory (plan 10)."""

from __future__ import annotations

import json
from pathlib import Path

from devsandbox.api.app import create_app

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "docs" / "openapi.json"


def test_committed_openapi_schema_is_current() -> None:
    expected = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    generated = create_app().openapi()
    assert expected == generated, (
        "docs/openapi.json is stale; run `python3 scripts/export_openapi.py`"
    )


def test_openapi_documents_every_v1_route() -> None:
    schema = create_app().openapi()
    paths = set(schema["paths"])
    assert "/v1/projects" in paths
    assert "/v1/projects/{project_id}/report" in paths
    assert "/v1/tasks/{task_id}/attempts/{attempt_number}/logs" in paths
    assert "/v1/system/status" in paths
    assert all(path.startswith("/v1/") for path in paths)
