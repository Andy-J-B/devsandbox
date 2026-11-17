"""Canary checks for every path that can persist or surface untrusted text.

The E2E suite has a full Docker run for prompts/logs/reports/DB/Git.  These
static checks cover the same boundaries without Docker so a regression is
caught by the fast gate as well.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.agent_helpers import make_request, make_workspace

from devsandbox.agents.prompt import build_prompt
from devsandbox.agents.sandbox_result import clean_summary, sanitize_log_file
from devsandbox.common.redaction import register_secret, reset_redactor
from devsandbox.common.types import TaskSpec
from devsandbox.orchestrator.events import emit_event

pytestmark = pytest.mark.security

CANARY = "canary-static-9f3a2b1c4d5e6f708192"


@pytest.fixture(autouse=True)
def _clean_redactor():
    reset_redactor()
    register_secret(CANARY)
    yield
    reset_redactor()


def test_prompt_redacts_canary_in_every_untrusted_field(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    task = TaskSpec(
        id=f"task-{CANARY}",
        title=f"Title {CANARY}",
        description=f"Description {CANARY}",
        instructions=f"Instructions {CANARY}",
        success_criteria=[f"criterion {CANARY}"],
        expected_files=[f"src/{CANARY}.py"],
        restricted_files=[f"docs/{CANARY}.md"],
    )
    request = make_request(
        workspace,
        task=task,
        prior_failure_summary=f"previous failure {CANARY}",
        context_instructions=f"context {CANARY}",
    )

    prompt = build_prompt(request)

    assert CANARY not in prompt
    assert "[REDACTED]" in prompt


def test_log_sanitizer_and_summary_redact_canary(tmp_path: Path) -> None:
    log_path = tmp_path / "stdout.log"
    log_path.write_text(f"agent output: {CANARY}\n", encoding="utf-8")

    sanitize_log_file(log_path)

    cleaned = log_path.read_text(encoding="utf-8")
    assert CANARY not in cleaned
    assert "[REDACTED]" in cleaned
    summary = clean_summary(f"summary includes {CANARY}")
    assert summary is not None
    assert CANARY not in summary


async def test_event_payload_redacts_canary_in_values_and_keys() -> None:
    class _CaptureEventRepo:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def add(self, event: Any) -> Any:
            self.events.append(event)
            return event

    repo = _CaptureEventRepo()
    await emit_event(
        None,  # type: ignore[arg-type]
        project_id="proj-canary",
        event_type="test.canary",
        payload={
            "note": f"leaked {CANARY}",
            f"key-{CANARY}": "value",
            "nested": {"list": [f"again {CANARY}"]},
        },
        event_repo=repo,  # type: ignore[arg-type]
    )

    assert len(repo.events) == 1
    serialized = str(repo.events[0].payload_json)
    assert CANARY not in serialized
    assert "[REDACTED]" in serialized
