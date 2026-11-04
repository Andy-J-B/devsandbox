"""Unit tests for common types and state enums."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from devsandbox.common.types import (
    AttemptOutcome,
    CheckSpec,
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


def test_check_spec_defaults() -> None:
    check = CheckSpec(name="tests", run="pytest -q")
    assert check.required is True
    assert check.timeout_seconds == 300
    assert check.working_dir == "."
    assert check.env == {}
    assert check.expected_exit_codes == [0]
    assert check.success_regex is None
    assert check.failure_regex is None
    assert check.junit_xml_path is None
    assert check.kind == "other"
    assert check.network == "none"
    assert check.allowed_hosts == []


def test_check_spec_accepts_argv_and_refinements() -> None:
    check = CheckSpec(
        name="tests",
        run=["python", "-m", "pytest", "-q"],
        kind="test",
        working_dir="backend",
        env={"CI": "1"},
        expected_exit_codes=[0, 5],
        success_regex="passed",
        failure_regex="failed",
        junit_xml_path="reports/junit.xml",
        network="full",
    )
    assert check.run == ["python", "-m", "pytest", "-q"]
    assert check.expected_exit_codes == [0, 5]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": " ", "run": "pytest"},
        {"name": "tests", "run": "   "},
        {"name": "tests", "run": []},
        {"name": "tests", "run": "pytest", "timeout_seconds": 0},
        {"name": "tests", "run": "pytest", "expected_exit_codes": []},
    ],
)
def test_check_spec_rejects_invalid_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CheckSpec.model_validate(kwargs)


def test_task_spec_carries_checks_and_modes() -> None:
    task = TaskSpec(
        id="t1",
        title="T",
        checks=[CheckSpec(name="tests", run="pytest")],
        allow_no_changes=True,
        expected_files_mode="fail",
    )
    assert task.checks[0].name == "tests"
    assert task.allow_no_changes is True
    assert task.expected_files_mode == "fail"
