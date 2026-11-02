"""Pure normalisation helpers: project defaults and policy clamps.

Planner output is untrusted.  These helpers fill in project-level defaults and
narrow every privilege-bearing value to the effective :class:`SecurityPolicy`
before validation or persistence.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..common.types import CheckSpec, ResourceProfile
from ..config.policy import SecurityPolicy
from ..config.settings import PlannerConfig
from .models import GraphIssue, GraphSeverity, TaskDraft


class NormalizationResult(BaseModel):
    """Normalised tasks plus warnings describing what was changed."""

    tasks: list[TaskDraft] = Field(default_factory=list)
    issues: list[GraphIssue] = Field(default_factory=list)


def _warning(code: str, issue: str, task_key: str) -> GraphIssue:
    return GraphIssue(
        code=code,
        severity=GraphSeverity.WARNING,
        task_key=task_key,
        issue=issue,
    )


def _ensure_required(checks: list[CheckSpec]) -> list[CheckSpec]:
    if any(check.required for check in checks):
        return checks
    return [
        *checks,
        CheckSpec(name="repo-sanity", run="git diff --check", required=True, timeout_seconds=300),
    ]


def apply_default_checks(
    tasks: list[TaskDraft], project_checks: list[CheckSpec]
) -> list[TaskDraft]:
    """Give tasks without checks the project-level validation commands."""
    if not project_checks:
        return [task.model_copy(deep=True) for task in tasks]
    defaults = _ensure_required(project_checks)
    normalised: list[TaskDraft] = []
    for task in tasks:
        if task.checks:
            normalised.append(task.model_copy(deep=True))
        else:
            normalised.append(
                task.model_copy(update={"checks": [check.model_copy() for check in defaults]})
            )
    return normalised


def apply_baseline_restrictions(
    tasks: list[TaskDraft], baseline_restricted_paths: list[str]
) -> list[TaskDraft]:
    """Ensure every task carries the project's baseline restricted paths."""
    if not baseline_restricted_paths:
        return [task.model_copy(deep=True) for task in tasks]
    normalised: list[TaskDraft] = []
    for task in tasks:
        restricted = list(task.restricted_files)
        for path in baseline_restricted_paths:
            if path not in restricted:
                restricted.append(path)
        normalised.append(task.model_copy(update={"restricted_files": restricted}))
    return normalised


def clamp_task(
    task: TaskDraft,
    *,
    policy: SecurityPolicy,
    planner_config: PlannerConfig,
    max_retries: int | None = None,
    max_timeout_seconds: int | None = None,
) -> tuple[TaskDraft, list[GraphIssue]]:
    """Clamp one task to the effective policy and return any clamp warnings."""
    issues: list[GraphIssue] = []
    update: dict[str, object] = {}

    requested = task.resources
    clamped = policy.effective_resources(requested)
    if clamped != requested:
        update["resources"] = clamped
        issues.append(
            _warning(
                "policy.resources_clamped",
                f"resources clamped from {requested.model_dump()} to {clamped.model_dump()}",
                task.key,
            )
        )

    if task.max_retries >= 0:
        retry_limit = policy.max_retries(task.max_retries)
        if max_retries is not None:
            retry_limit = min(retry_limit, max_retries)
        if retry_limit != task.max_retries:
            update["max_retries"] = retry_limit
            issues.append(
                _warning(
                    "policy.retries_clamped",
                    f"max_retries clamped from {task.max_retries} to {retry_limit}",
                    task.key,
                )
            )

    if task.timeout_seconds > 0:
        timeout_limit = policy.effective_timeout(task.timeout_seconds)
        if max_timeout_seconds is not None:
            timeout_limit = min(timeout_limit, max_timeout_seconds)
        if timeout_limit != task.timeout_seconds:
            update["timeout_seconds"] = timeout_limit
            issues.append(
                _warning(
                    "policy.timeout_clamped",
                    f"timeout clamped from {task.timeout_seconds}s to {timeout_limit}s",
                    task.key,
                )
            )

    priority = min(max(task.priority, planner_config.min_priority), planner_config.max_priority)
    if priority != task.priority:
        update["priority"] = priority
        issues.append(
            _warning(
                "policy.priority_clamped",
                f"priority clamped from {task.priority} to {priority}",
                task.key,
            )
        )

    if not update:
        return task.model_copy(deep=True), issues
    return task.model_copy(update=update), issues


def normalize_tasks(
    tasks: list[TaskDraft],
    *,
    policy: SecurityPolicy,
    planner_config: PlannerConfig,
    project_checks: list[CheckSpec] | None = None,
    baseline_restricted_paths: list[str] | None = None,
    max_retries: int | None = None,
    max_timeout_seconds: int | None = None,
) -> NormalizationResult:
    """Apply project defaults and policy clamps to a list of task drafts."""
    normalised = apply_default_checks(tasks, project_checks or [])
    normalised = apply_baseline_restrictions(normalised, baseline_restricted_paths or [])
    issues: list[GraphIssue] = []
    clamped: list[TaskDraft] = []
    for task in normalised:
        task, task_issues = clamp_task(
            task,
            policy=policy,
            planner_config=planner_config,
            max_retries=max_retries,
            max_timeout_seconds=max_timeout_seconds,
        )
        clamped.append(task)
        issues.extend(task_issues)
    return NormalizationResult(tasks=clamped, issues=issues)


def policy_resource_ceiling(policy: SecurityPolicy) -> ResourceProfile:
    """Return the policy's maximum resource profile."""
    return policy.max_resources()


__all__ = [
    "NormalizationResult",
    "apply_baseline_restrictions",
    "apply_default_checks",
    "clamp_task",
    "normalize_tasks",
    "policy_resource_ceiling",
]
