"""Read-only query helpers shared by API routes."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.errors import PlanNotFoundError, ProjectNotFoundError, TaskNotFoundError
from ..common.ids import TaskId
from ..common.types import CheckSpec, ProjectMode, RepoSource, ResourceProfile, TaskStatus
from ..db.models import AttemptModel, EventModel, PlanModel, ProjectModel, TaskModel
from ..db.repositories import AttemptRepo, ProjectRepo, TaskRepo
from ..reporting.models import ReportCounts
from .schemas import (
    AttemptDetail,
    AttemptSummary,
    EventRecord,
    ProjectSummary,
    TaskDetail,
    TaskSummary,
)

_COUNT_FIELDS: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "pending",
    TaskStatus.READY: "ready",
    TaskStatus.SCHEDULED: "running",
    TaskStatus.RUNNING: "running",
    TaskStatus.VALIDATING: "running",
    TaskStatus.VALIDATED: "running",
    TaskStatus.INTEGRATING: "running",
    TaskStatus.RETRY_WAIT: "retry_wait",
    TaskStatus.COMPLETED: "completed",
    TaskStatus.FAILED: "failed",
    TaskStatus.BLOCKED: "blocked",
    TaskStatus.CANCELLED: "cancelled",
}


def json_dict(value: Any) -> dict[str, Any]:
    """Return a JSON object from a dict or serialized JSON string."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def duration(started: datetime | None, finished: datetime | None) -> float:
    """Return non-negative duration in seconds."""
    if started is None or finished is None:
        return 0.0
    return max(0.0, (finished - started).total_seconds())


def repo_source_label(project: ProjectModel) -> str | None:
    """Return a display label for the project repository."""
    raw = json_dict(project.repo_source_json)
    if raw:
        if raw.get("path"):
            return str(raw["path"])
        if raw.get("url"):
            return str(raw["url"])
    return project.repo_path


def _repo_source(project: ProjectModel) -> RepoSource | None:
    raw = json_dict(project.repo_source_json)
    if not raw:
        return None
    try:
        return RepoSource.model_validate(raw)
    except ValueError:
        return None


async def load_project(session: AsyncSession, project_id: str) -> ProjectModel:
    """Load a project or raise :class:`ProjectNotFoundError`."""
    project = await ProjectRepo(session).get(project_id)
    if project is None:
        raise ProjectNotFoundError(f"project {project_id} not found")
    return project


async def load_task(session: AsyncSession, task_id: str) -> TaskModel:
    """Load a task or raise :class:`TaskNotFoundError`."""
    task = await TaskRepo(session).get(TaskId(task_id))
    if task is None:
        raise TaskNotFoundError(f"task {task_id} not found")
    return task


async def load_plan(session: AsyncSession, plan_id: str) -> PlanModel:
    """Load a plan or raise :class:`PlanNotFoundError`."""
    result = await session.execute(select(PlanModel).where(PlanModel.id == plan_id))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise PlanNotFoundError(f"plan {plan_id} not found")
    return plan


async def project_tasks(session: AsyncSession, project: ProjectModel) -> list[TaskModel]:
    """Return tasks from the active plan (falling back to the newest rows)."""
    repo = TaskRepo(session)
    tasks = await repo.list_for_project(project.id, plan_id=project.active_plan_id)
    if tasks or project.active_plan_id is not None:
        return tasks
    return await repo.list_for_project(project.id)


def task_counts(tasks: list[TaskModel]) -> ReportCounts:
    """Count tasks by lifecycle bucket."""
    values: dict[str, int] = {}
    for task in tasks:
        field = _COUNT_FIELDS.get(TaskStatus(task.status), "other")
        values[field] = values.get(field, 0) + 1
    return ReportCounts().model_copy(update={"total": len(tasks), **values})


async def project_view(session: AsyncSession, project: ProjectModel) -> ProjectSummary:
    """Build a project row with task counts."""
    tasks = await project_tasks(session, project)
    return ProjectSummary(
        id=project.id,
        name=project.name,
        mode=ProjectMode(project.mode),
        status=project.status,
        spec_hash=project.spec_hash,
        repo_source=repo_source_label(project),
        repo_path=project.repo_path,
        main_branch=project.main_branch,
        staging_branch=project.staging_branch,
        active_plan_id=project.active_plan_id,
        counts=task_counts(tasks),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def attempt_summary(attempt: AttemptModel) -> AttemptSummary:
    """Convert an attempt row into its summary response."""
    report = json_dict(attempt.validation_report_json)
    return AttemptSummary(
        id=attempt.id,
        task_id=attempt.task_id,
        number=attempt.number,
        status=attempt.status,
        exit_status=attempt.exit_status,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        duration_seconds=duration(attempt.started_at, attempt.finished_at),
        base_commit=attempt.base_commit,
        head_commit=attempt.head_commit,
        branch=attempt.branch,
        workspace_path=attempt.workspace_path,
        container_id=attempt.container_id,
        agent_name=attempt.agent_name,
        agent_log_path=attempt.agent_log_path,
        validation_status=(str(report["status"]) if report.get("status") is not None else None),
        validation_summary=str(report.get("summary") or ""),
        failure=json_dict(attempt.failure_json) or None,
        token_usage=json_dict(attempt.token_usage_json) or None,
        peak_memory_mb=attempt.peak_memory_mb,
        created_at=attempt.created_at,
        updated_at=attempt.updated_at,
    )


def attempt_detail(attempt: AttemptModel) -> AttemptDetail:
    """Convert an attempt row into its full response."""
    report = json_dict(attempt.validation_report_json)
    checks = report.get("checks")
    if not isinstance(checks, list):
        checks = []
    summary = attempt_summary(attempt)
    return AttemptDetail(
        **summary.model_dump(),
        validation_report=report or None,
        checks=[item for item in checks if isinstance(item, dict)],
        failure=json_dict(attempt.failure_json) or None,
    )


def _checks(task: TaskModel) -> list[CheckSpec]:
    parsed: list[CheckSpec] = []
    for item in task.checks_json or []:
        try:
            parsed.append(CheckSpec.model_validate(item))
        except ValueError:
            continue
    return parsed


def task_summary(
    task: TaskModel,
    *,
    attempts: list[AttemptModel],
    dependencies: list[str],
) -> TaskSummary:
    """Convert a task row into its list response."""
    return TaskSummary(
        id=task.id,
        project_id=task.project_id,
        plan_id=task.plan_id,
        key=task.key,
        title=task.title,
        status=task.status,
        priority=task.priority,
        attempt_count=task.attempt_count,
        max_retries=task.max_retries,
        current_attempt_id=task.current_attempt_id,
        blocked_reason=task.blocked_reason,
        failure=json_dict(task.failure_json) or None,
        attempts=[attempt_summary(attempt) for attempt in attempts],
        dependencies=dependencies,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def task_detail(
    task: TaskModel,
    *,
    attempts: list[AttemptModel],
    dependencies: list[str],
) -> TaskDetail:
    """Convert a task row into its full response."""
    summary = task_summary(task, attempts=attempts, dependencies=dependencies)
    return TaskDetail(
        **summary.model_dump(),
        description=task.description or "",
        instructions=task.instructions or "",
        success_criteria=list(task.success_criteria_json or []),
        checks=_checks(task),
        expected_files=list(task.expected_files_json or []),
        restricted_files=list(task.restricted_files_json or []),
        resources=ResourceProfile.model_validate(task.resource_profile_json or {}),
        timeout_seconds=task.timeout_seconds,
        retry_not_before=task.retry_not_before,
        result=json_dict(task.result_json) or None,
    )


async def task_attempts(session: AsyncSession, task_id: str) -> list[AttemptModel]:
    """Return attempts for a task in execution order."""
    return await AttemptRepo(session).list_for_task(task_id)


def event_record(event: EventModel) -> EventRecord:
    """Convert an event row into its response."""
    return EventRecord(
        id=event.id,
        project_id=event.project_id,
        task_id=event.task_id,
        attempt_id=event.attempt_id,
        type=event.type,
        from_state=event.from_state,
        to_state=event.to_state,
        payload=json_dict(event.payload_json),
        at=event.at,
    )


__all__ = [
    "attempt_detail",
    "attempt_summary",
    "duration",
    "event_record",
    "json_dict",
    "load_plan",
    "load_project",
    "load_task",
    "project_tasks",
    "project_view",
    "repo_source_label",
    "task_attempts",
    "task_counts",
    "task_detail",
    "task_summary",
]
