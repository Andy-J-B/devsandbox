"""Helpers shared by planner tests."""

from __future__ import annotations

from devsandbox.common.types import CheckSpec, RepoSource, ResourceProfile
from devsandbox.planner.models import TaskDraft
from devsandbox.spec.models import ProjectSpec, ValidationSpec


def make_draft(
    key: str = "task-a",
    *,
    title: str | None = None,
    depends_on: list[str] | None = None,
    expected_files: list[str] | None = None,
    restricted_files: list[str] | None = None,
    priority: int = 50,
    timeout_seconds: int = 1800,
    max_retries: int = 0,
    checks: list[CheckSpec] | None = None,
    resources: ResourceProfile | None = None,
    instructions: str | None = None,
    success_criteria: list[str] | None = None,
) -> TaskDraft:
    """Return a task draft that satisfies the default validation rules."""
    return TaskDraft(
        key=key,
        title=title or key,
        description=f"Task {key}",
        instructions=instructions if instructions is not None else f"Implement {key}",
        depends_on=list(depends_on or []),
        priority=priority,
        success_criteria=list(
            success_criteria if success_criteria is not None else ["The task is complete"]
        ),
        checks=list(
            checks
            if checks is not None
            else [CheckSpec(name="tests", run="pytest -q", required=True, timeout_seconds=60)]
        ),
        expected_files=list(expected_files or []),
        restricted_files=list(restricted_files or []),
        resources=resources or ResourceProfile(),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )


def make_spec(
    *,
    name: str = "demo",
    goals: list[str] | None = None,
    requirements: list[str] | None = None,
    commands: list[CheckSpec] | None = None,
    restricted_paths: list[str] | None = None,
    max_retries: int = 2,
    task_timeout_seconds: int = 1800,
) -> ProjectSpec:
    """Return a minimal valid project spec."""
    return ProjectSpec(
        name=name,
        repo=RepoSource(path="/tmp/devsandbox-demo-repo"),
        goals=list(goals if goals is not None else ["Add feature A"]),
        requirements=list(requirements or []),
        constraints={
            "languages": ["python"],
            "restricted_paths": list(restricted_paths or []),
        },
        validation=ValidationSpec(commands=list(commands or [])),
        limits={
            "max_retries": max_retries,
            "task_timeout_seconds": task_timeout_seconds,
        },
    )
