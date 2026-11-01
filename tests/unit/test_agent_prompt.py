"""Unit tests for the deterministic agent prompt builder."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.agent_helpers import make_request, make_workspace

from devsandbox.agents.prompt import build_prompt
from devsandbox.common.redaction import register_secret, reset_redactor
from devsandbox.common.types import TaskSpec


@pytest.fixture(autouse=True)
def _clean_redactor():
    reset_redactor()
    yield
    reset_redactor()


def test_prompt_is_deterministic(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    req = make_request(workspace)
    assert build_prompt(req) == build_prompt(req)


def test_prompt_contains_task_sections_and_rules(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    req = make_request(
        workspace,
        prior_failure_summary="check `unit` failed: 1 test failed",
        attempt_number=2,
    )
    prompt = build_prompt(req)

    assert "# DevSandbox coding task" in prompt
    assert "Implement the feature" in prompt
    assert "Edit the repository and commit." in prompt
    assert "tests pass" in prompt
    assert "pytest -q" in prompt  # validation command
    assert "src/feature.py" in prompt  # expected file
    assert ".github/workflows/**" in prompt  # restricted file
    assert "Attempt number: 2" in prompt
    assert "check `unit` failed" in prompt
    assert "Work only inside `/workspace`" in prompt
    assert "Commit your work with git" in prompt
    assert "Never print, commit, log" in prompt
    assert "src/main.py" in prompt


def test_prompt_includes_retry_failure_summary_only_on_retry(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    first = build_prompt(make_request(workspace))
    retry = build_prompt(
        make_request(
            workspace, prior_failure_summary="previous attempt timed out", attempt_number=2
        )
    )
    assert "previous attempt timed out" not in first
    assert "previous attempt timed out" in retry


def test_prompt_enforces_size_cap_and_truncation_marker(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    task = TaskSpec(
        id="task-1",
        title="Huge task",
        description="d" * 50_000,
        instructions="i" * 50_000,
        success_criteria=["c" * 5_000],
    )
    prompt = build_prompt(make_request(workspace, task=task), max_chars=8_000)
    assert len(prompt) <= 8_000
    assert "truncated" in prompt


def test_prompt_redacts_registered_secrets(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    canary = "canary-secret-value-abc123xyz"
    register_secret(canary)
    req = make_request(workspace, context_instructions=f"secret is {canary}")
    prompt = build_prompt(req)
    assert canary not in prompt
    assert "[REDACTED]" in prompt


def test_prompt_rejects_tiny_budget(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    with pytest.raises(ValueError):
        build_prompt(make_request(workspace), max_chars=10)
