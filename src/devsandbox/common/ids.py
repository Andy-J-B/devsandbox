"""Identifier generation and NewType aliases for DevSandbox."""

from __future__ import annotations

import uuid
from typing import NewType

ProjectId = NewType("ProjectId", str)
TaskId = NewType("TaskId", str)
AttemptId = NewType("AttemptId", str)
PlanId = NewType("PlanId", str)


def _new_id() -> str:
    return uuid.uuid4().hex


def new_project_id() -> ProjectId:
    """Generate a new project identifier."""
    return ProjectId(_new_id())


def new_task_id() -> TaskId:
    """Generate a new task identifier."""
    return TaskId(_new_id())


def new_attempt_id() -> AttemptId:
    """Generate a new attempt identifier."""
    return AttemptId(_new_id())


def new_plan_id() -> PlanId:
    """Generate a new plan identifier."""
    return PlanId(_new_id())
