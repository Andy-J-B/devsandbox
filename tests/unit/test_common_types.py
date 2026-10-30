"""Unit tests for common types and state enums."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from devsandbox.common.types import (
    AttemptOutcome,
    NetworkPolicy,
    ResourceProfile,
    TaskSpec,
    TaskStatus,
)


def test_task_status_values() -> None:
    assert TaskStatus.PENDING.value == "pending"
    assert TaskStatus.COMPLETED.value == "completed"
    assert TaskStatus.RETRY_WAIT.value == "retry_wait"
    assert TaskStatus.BLOCKED.value == "blocked"


def test_attempt_outcome_values() -> None:
    assert AttemptOutcome.EXITED_OK.value == "exited_ok"
    assert AttemptOutcome.TIMEOUT.value == "timeout"
    assert AttemptOutcome.OOM.value == "oom"


def test_resource_profile_frozen() -> None:
    profile = ResourceProfile(cpu_cores=2.0)
    with pytest.raises(ValidationError):
        profile.cpu_cores = 4.0  # type: ignore[misc]


def test_task_spec_defaults() -> None:
    task = TaskSpec(id="t1", title="T")
    assert task.timeout_seconds == 1800
    assert task.max_retries == 0
    assert task.success_criteria == []
    assert task.restricted_files == []


def test_network_policy_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        NetworkPolicy(mode="sometimes")  # type: ignore[arg-type]
