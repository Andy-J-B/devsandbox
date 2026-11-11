"""Tests for shared orchestrator conversion helpers."""

from __future__ import annotations

from devsandbox.common.types import TaskStatus
from devsandbox.db.models import TaskModel
from devsandbox.orchestrator.support import task_spec_from_model


def _task(**overrides: object) -> TaskModel:
    values: dict[str, object] = {
        "id": "task-1",
        "project_id": "proj-1",
        "plan_id": "plan-1",
        "key": "verify",
        "title": "Verify",
        "status": TaskStatus.READY.value,
        "timeout_seconds": 1800,
        "max_retries": 0,
        "version": 1,
    }
    values.update(overrides)
    return TaskModel(**values)


def test_task_spec_from_model_preserves_allow_no_changes() -> None:
    task = _task(allow_no_changes=True)

    spec = task_spec_from_model(task)

    assert spec.allow_no_changes is True


def test_task_spec_from_model_defaults_allow_no_changes_to_false() -> None:
    task = _task(allow_no_changes=False)

    spec = task_spec_from_model(task)

    assert spec.allow_no_changes is False
