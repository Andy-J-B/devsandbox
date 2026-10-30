"""Planner protocol and task graph validation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.types import ProjectContext, RepoProfile
from ..spec.models import ProjectSpec
from .types import FailureRecord, GraphIssue, Plan, ProposedPlan, TaskDraft

PlannerName = str


def validate_graph(tasks: list[TaskDraft]) -> list[GraphIssue]:
    """Validate a task graph and return the list of issues found."""
    issues: list[GraphIssue] = []
    keys: list[str] = []
    for task in tasks:
        if task.key in keys:
            issues.append(GraphIssue(task_key=task.key, issue="duplicate task key"))
        keys.append(task.key)

    known = set(keys)
    for task in tasks:
        for dep in task.depends_on:
            if dep == task.key:
                issues.append(GraphIssue(task_key=task.key, issue="task depends on itself"))
            if dep not in known:
                issues.append(GraphIssue(task_key=task.key, issue=f"unknown dependency {dep}"))
    return issues


@runtime_checkable
class Planner(Protocol):
    """Protocol implemented by the task planner."""

    async def generate_plan(
        self, spec: ProjectSpec, profile: RepoProfile, ctx: ProjectContext
    ) -> ProposedPlan: ...

    async def replan(
        self, project_id: str, plan: Plan, failures: list[FailureRecord]
    ) -> ProposedPlan: ...
