"""Planner protocol and task graph validation types."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..common.types import CheckSpec, ResourceProfile


class TaskDraft(BaseModel):
    """A task draft produced by the planner."""

    key: str
    title: str
    description: str = ""
    instructions: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)
    restricted_files: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    resources: ResourceProfile = Field(default_factory=ResourceProfile)
    timeout_seconds: int = 1800
    max_retries: int = 0


class ProposedPlan(BaseModel):
    """A proposed task graph."""

    tasks: list[TaskDraft] = Field(default_factory=list)
    rationale: str = ""


class Plan(BaseModel):
    """An accepted task graph version."""

    id: str
    version: int
    status: str
    tasks: list[TaskDraft] = Field(default_factory=list)


class FailureRecord(BaseModel):
    """Record of a failed task attempt, used for replanning."""

    task_id: str
    attempt_id: str
    summary: str | None = None
    error: str = ""


class GraphIssue(BaseModel):
    """A structural issue detected in a task graph."""

    task_key: str
    issue: str
