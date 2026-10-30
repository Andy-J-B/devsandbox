"""Shared DevSandbox test factory helpers."""

from __future__ import annotations

from devsandbox.common.ids import AttemptId, PlanId, ProjectId, TaskId
from devsandbox.common.types import (
    AttemptOutcome,
    ContextBundle,
    NetworkPolicy,
    ProjectContext,
    RepoProfile,
    ResourceProfile,
    TaskOutcome,
    TaskSpec,
)
from devsandbox.planner.types import Plan, ProposedPlan, TaskDraft


def make_task(
    task_id: str = "task-1",
    title: str = "Do the thing",
    instructions: str = "Make it work",
) -> TaskSpec:
    """Return a TaskSpec factory argument bundle."""
    return TaskSpec(id=task_id, title=title, instructions=instructions)


def make_task_spec(task_id: str = "task-1") -> TaskSpec:
    """Return a minimal TaskSpec."""
    return TaskSpec(id=task_id, title="Do the thing", instructions="Make it work")


def make_plan_with_tasks(task_keys: list[str]) -> Plan:
    """Return a fake plan containing the given task keys."""
    tasks = [TaskDraft(key=key, title=key) for key in task_keys]
    return Plan(id="plan-1", version=1, status="proposed", tasks=tasks)


def make_proposed_plan(task_keys: list[str], rationale: str = "fake") -> ProposedPlan:
    """Return a fake proposed plan."""
    return ProposedPlan(
        tasks=[TaskDraft(key=key, title=key) for key in task_keys], rationale=rationale
    )


def make_context_bundle(task_id: str, task: TaskSpec) -> ContextBundle:
    """Return a fake context bundle."""
    return ContextBundle(
        task_id=task_id, task=task, context_files=["src/main.py"], instructions=task.instructions
    )


def make_project_context() -> ProjectContext:
    """Return a fake project context."""
    return ProjectContext(
        goals=["ship it"], requirements=["tests pass"], constraints=["no network"]
    )


def make_repo_profile() -> RepoProfile:
    """Return a fake repo profile."""
    return RepoProfile(name="fake-repo", languages=["python"], test_commands=["pytest -q"])


def make_task_attempt_id() -> AttemptId:
    """Return a fresh attempt id."""
    from devsandbox.common.ids import new_attempt_id

    return new_attempt_id()


__all__ = [
    "AttemptId",
    "AttemptOutcome",
    "ContextBundle",
    "NetworkPolicy",
    "PlanId",
    "ProjectContext",
    "ProjectId",
    "RepoProfile",
    "ResourceProfile",
    "TaskId",
    "TaskOutcome",
    "TaskSpec",
    "make_context_bundle",
    "make_plan_with_tasks",
    "make_project_context",
    "make_proposed_plan",
    "make_repo_profile",
    "make_task",
    "make_task_attempt_id",
    "make_task_spec",
]
