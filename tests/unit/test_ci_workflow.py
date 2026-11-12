"""Guardrails for the manual OpenCode smoke CI job.

The live smoke test is easy to disable accidentally: it needs an explicit
live flag, a provider/model, and an agent image tag that matches the one the
pytest fixture expects.  These assertions keep the workflow wiring honest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _opencode_job() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["opencode-smoke"]


def test_opencode_smoke_job_enables_live_model_run() -> None:
    job = _opencode_job()
    env = job["env"]

    assert job["if"] == "github.event_name == 'workflow_dispatch'"
    assert env["DEVSANDBOX_RUN_OPENCODE_INTEGRATION"] == "1"
    assert "inputs.opencode_live" in env["DEVSANDBOX_OPENCODE_LIVE"]
    assert "inputs.opencode_provider" in env["DEVSANDBOX_OPENCODE_PROVIDER"]
    assert "'opencode'" in env["DEVSANDBOX_OPENCODE_PROVIDER"]
    assert "inputs.opencode_model" in env["DEVSANDBOX_OPENCODE_MODEL"]
    assert "'deepseek-v4.1-flash-free'" in env["DEVSANDBOX_OPENCODE_MODEL"]
    assert env["DEVSANDBOX_OPENCODE_NETWORK"] == "full"
    assert env["DEVSANDBOX_OPENCODE_API_KEY"] == "${{ secrets.OPENCODE_API_KEY }}"


def test_opencode_smoke_job_builds_the_image_tag_the_tests_use() -> None:
    job = _opencode_job()
    env = job["env"]

    assert env["DEVSANDBOX_AGENT_IMAGE"] == "devsandbox/agent:test"
    build_step = next(step for step in job["steps"] if step.get("name") == "build agent image")
    assert 'IMAGE="$DEVSANDBOX_AGENT_IMAGE"' in build_step["run"]
    assert "docker/agent-base/build.sh" in build_step["run"]

    test_step = next(
        step for step in job["steps"] if step.get("name") == "real OpenCode smoke test"
    )
    assert "-m opencode" in test_step["run"]
