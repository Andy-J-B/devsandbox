"""Read-only project report builder (plan 10).

The builder reads persisted state only.  It never mutates the database, never
calls an agent, and never performs a git integration.  ``staging_head`` is the
one optional external read; it is skipped when no git manager is supplied.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..common.errors import ProjectNotFoundError
from ..common.redaction import redact
from ..common.time import utcnow
from ..common.types import ProjectStatus, RepoSource, TaskStatus
from ..config.settings import DevSandboxSettings, get_settings
from ..db.models import AttemptModel, EventModel, IntegrationModel, ProjectModel, TaskModel
from ..db.repositories import (
    AttemptRepo,
    CostRepo,
    IntegrationRepo,
    PlanRepo,
    ProjectRepo,
    TaskRepo,
)
from ..gitmgr.interfaces import GitManager
from ..orchestrator.support import repo_handle_for_project
from .models import (
    ProjectReport,
    ReportAttempt,
    ReportCheckResult,
    ReportCommit,
    ReportCosts,
    ReportCounts,
    ReportFailure,
    ReportHeader,
    ReportIntegration,
    ReportOutstanding,
    ReportResourceEvent,
    ReportTask,
    ReportTestAggregate,
)

_ACTIVE_EVENT_TYPES = {
    "RESOURCE_PRESSURE",
    "CIRCUIT_BREAKER",
    "POLICY_DENIED",
    "project.needs_attention",
    "SCHEDULE_DECISION",
}
_TERMINAL_TASK_STATES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
    TaskStatus.CANCELLED,
}
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


def _duration(started: datetime | None, finished: datetime | None) -> float:
    if started is None or finished is None:
        return 0.0
    return max(0.0, (finished - started).total_seconds())


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _failure(raw: Any) -> ReportFailure | None:
    data = _json_dict(raw)
    if not data:
        return None
    detail = _json_dict(data.get("detail"))
    pointer = data.get("diagnostics_path")
    return ReportFailure(
        kind=str(data.get("kind") or data.get("failure_kind") or "unknown"),
        summary=str(data.get("summary") or ""),
        detail=detail,
        log_tail_pointer=str(pointer) if pointer else None,
    )


def _commits(raw: Any) -> list[ReportCommit]:
    if not isinstance(raw, list):
        return []
    commits: list[ReportCommit] = []
    for item in raw:
        if isinstance(item, str):
            commits.append(ReportCommit(sha=item))
        elif isinstance(item, dict):
            sha = item.get("sha") or item.get("hash") or item.get("commit")
            if sha:
                commits.append(
                    ReportCommit(
                        sha=str(sha),
                        subject=str(item.get("subject") or item.get("message") or ""),
                    )
                )
    return commits


def _checks(report: dict[str, Any]) -> list[ReportCheckResult]:
    checks: list[ReportCheckResult] = []
    for item in report.get("checks") or []:
        if not isinstance(item, dict):
            continue
        checks.append(
            ReportCheckResult(
                name=str(item.get("check") or item.get("name") or "check"),
                status=str(item.get("status") or ("passed" if item.get("passed") else "failed")),
                passed=bool(item.get("passed", item.get("status") == "passed")),
                required=bool(item.get("required", True)),
                duration_seconds=float(item.get("duration_seconds") or 0.0),
                exit_code=item.get("exit_code"),
                tests_total=item.get("tests_total"),
                tests_failed=item.get("tests_failed"),
                tests_skipped=item.get("tests_skipped"),
                tests_errors=item.get("tests_errors"),
                failure_kind=(
                    str(item.get("failure_kind")) if item.get("failure_kind") is not None else None
                ),
                summary=str(item.get("reason") or item.get("output") or ""),
            )
        )
    return checks


def _validation_report(attempt: AttemptModel) -> dict[str, Any]:
    return _json_dict(attempt.validation_report_json)


def _task_counts(tasks: Iterable[TaskModel]) -> ReportCounts:
    counts = ReportCounts()
    total = 0
    values: dict[str, int] = {}
    for task in tasks:
        total += 1
        field = _COUNT_FIELDS.get(TaskStatus(task.status), "other")
        values[field] = values.get(field, 0) + 1
    return counts.model_copy(update={"total": total, **values})


def _repo_source(project: ProjectModel) -> str | None:
    raw = _json_dict(project.repo_source_json)
    if not raw:
        return project.repo_path
    if raw.get("path"):
        return str(raw["path"])
    if raw.get("url"):
        return str(raw["url"])
    return project.repo_path


def _attempt_report(
    attempt: AttemptModel,
    *,
    subject_by_sha: dict[str, str],
) -> ReportAttempt:
    report = _validation_report(attempt)
    checks = _checks(report)
    commits: list[ReportCommit] = []
    if attempt.head_commit:
        commits.append(
            ReportCommit(
                sha=attempt.head_commit,
                subject=subject_by_sha.get(attempt.head_commit, ""),
            )
        )
    files_changed = report.get("files_changed")
    if not isinstance(files_changed, list):
        files_changed = []
    return ReportAttempt(
        number=attempt.number,
        status=attempt.status,
        outcome=attempt.exit_status or attempt.status,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        duration_seconds=_duration(attempt.started_at, attempt.finished_at),
        exit_status=attempt.exit_status,
        base_commit=attempt.base_commit,
        head_commit=attempt.head_commit,
        commits=commits,
        files_changed=[str(path) for path in files_changed],
        checks=checks,
        validation_status=report.get("status"),
        validation_summary=str(report.get("summary") or ""),
        failure=_failure(attempt.failure_json),
        token_usage=_json_dict(attempt.token_usage_json) or None,
        peak_memory_mb=attempt.peak_memory_mb,
        log_pointer=attempt.agent_log_path,
    )


def _task_report(
    task: TaskModel,
    *,
    attempts: list[AttemptModel],
    dependencies: list[str],
) -> ReportTask:
    result = _json_dict(task.result_json)
    commits = _commits(result.get("commits"))
    subject_by_sha = {commit.sha: commit.subject for commit in commits}
    attempt_reports = [
        _attempt_report(attempt, subject_by_sha=subject_by_sha) for attempt in attempts
    ]
    for attempt in attempts:
        if attempt.head_commit and not any(c.sha == attempt.head_commit for c in commits):
            commits.append(
                ReportCommit(
                    sha=attempt.head_commit,
                    subject=subject_by_sha.get(attempt.head_commit, ""),
                )
            )
    latest = attempts[-1] if attempts else None
    checks = _checks(_validation_report(latest)) if latest is not None else []
    files_changed = result.get("files_changed")
    if not isinstance(files_changed, list):
        files_changed = []
    if not files_changed and attempt_reports:
        files_changed = list(attempt_reports[-1].files_changed)
    duration = sum(attempt.duration_seconds for attempt in attempt_reports)
    if duration == 0.0 and TaskStatus(task.status) in _TERMINAL_TASK_STATES:
        duration = _duration(task.created_at, task.updated_at)
    return ReportTask(
        id=task.id,
        key=task.key,
        title=task.title,
        status=task.status,
        priority=task.priority,
        attempt_count=task.attempt_count,
        max_retries=task.max_retries,
        duration_seconds=duration,
        attempts=attempt_reports,
        commits=commits,
        files_changed=[str(path) for path in files_changed],
        checks=checks,
        failure=_failure(task.failure_json),
        blocked_by=dependencies,
        blocked_reason=task.blocked_reason,
        result=result,
    )


def _integration_report(row: IntegrationModel) -> ReportIntegration:
    conflicts = row.conflict_files_json or []
    return ReportIntegration(
        id=row.id,
        task_id=row.task_id,
        attempt_id=row.attempt_id,
        status=row.status,
        staging_before=row.staging_commit_before,
        staging_after=row.staging_commit_after,
        conflicting_files=[str(path) for path in conflicts],
        detail=row.detail or "",
        at=row.at,
    )


def _test_aggregates(tasks: list[ReportTask]) -> list[ReportTestAggregate]:
    buckets: dict[str, dict[str, int]] = {}
    for task in tasks:
        for attempt in task.attempts:
            for check in attempt.checks:
                bucket = buckets.setdefault(check.name, {})
                status = (
                    check.status
                    if check.status in {"passed", "failed", "timeout", "error"}
                    else ("skipped" if check.status == "skipped" else "error")
                )
                bucket[status] = bucket.get(status, 0) + 1
                bucket["total"] = bucket.get("total", 0) + 1
    return [
        ReportTestAggregate(
            check=name,
            passed=values.get("passed", 0),
            failed=values.get("failed", 0),
            timeout=values.get("timeout", 0),
            error=values.get("error", 0),
            skipped=values.get("skipped", 0),
            total=values.get("total", 0),
        )
        for name, values in sorted(buckets.items())
    ]


def _event_detail(payload: dict[str, Any]) -> str:
    for key in ("reason_code", "reason", "detail", "summary", "message", "error"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _resource_events(
    events: list[EventModel], tasks: list[ReportTask]
) -> list[ReportResourceEvent]:
    result: list[ReportResourceEvent] = []
    for event in events:
        if event.type not in _ACTIVE_EVENT_TYPES:
            continue
        payload = _json_dict(event.payload_json)
        if event.type == "SCHEDULE_DECISION" and payload.get("action") != "defer":
            continue
        result.append(
            ReportResourceEvent(
                type=event.type,
                task_id=event.task_id,
                attempt_id=event.attempt_id,
                at=event.at,
                detail=_event_detail(payload),
            )
        )
    for task in tasks:
        for attempt in task.attempts:
            if attempt.exit_status == "timeout":
                result.append(
                    ReportResourceEvent(
                        type="attempt.timeout",
                        task_id=task.id,
                        at=attempt.finished_at,
                        detail=f"attempt {attempt.number} timed out",
                    )
                )
            elif attempt.exit_status == "oom":
                result.append(
                    ReportResourceEvent(
                        type="attempt.oom",
                        task_id=task.id,
                        at=attempt.finished_at,
                        detail=f"attempt {attempt.number} was OOM-killed",
                    )
                )
    return result


def _warnings(events: list[EventModel], tasks: list[ReportTask]) -> list[str]:
    warnings: list[str] = []
    for event in events:
        payload = _json_dict(event.payload_json)
        if event.type == "validation.baseline" and payload.get("status") not in {
            None,
            "passed",
        }:
            summary = str(payload.get("summary") or "baseline validation failed")
            warnings.append(f"baseline validation: {summary}")
        elif event.type == "POLICY_DENIED":
            warnings.append(f"policy denial: {_event_detail(payload) or 'operation denied'}")
        elif event.type == "project.needs_attention":
            warnings.append(f"needs attention: {_event_detail(payload) or 'manual review'}")
    for task in tasks:
        for attempt in task.attempts:
            if attempt.failure and attempt.failure.kind == "tampering":
                warnings.append(
                    f"test tampering signal in task {task.key}: {attempt.failure.summary}"
                )
    return sorted(set(warnings))


def _outstanding(tasks: list[ReportTask], warnings: list[str]) -> ReportOutstanding:
    unresolved: list[str] = []
    blocked: list[str] = []
    actions: list[str] = []
    for report_task in tasks:
        if report_task.status == TaskStatus.FAILED.value:
            reason = (
                report_task.failure.summary if report_task.failure is not None else "task failed"
            )
            unresolved.append(f"{report_task.key}: {reason}")
            actions.append(f"inspect task {report_task.key} logs and retry or replan")
        elif report_task.status == TaskStatus.BLOCKED.value:
            reason = report_task.blocked_reason or "dependency failed"
            blocked.append(f"{report_task.key}: {reason}")
            actions.append(f"resolve the failed dependency of task {report_task.key}")
        elif report_task.status == TaskStatus.CANCELLED.value:
            unresolved.append(f"{report_task.key}: cancelled")
    for report_task in tasks:
        for attempt in report_task.attempts:
            if attempt.failure and attempt.failure.kind == "git_conflict":
                actions.append(f"rebase task {report_task.key} onto the current staging head")
    return ReportOutstanding(
        unresolved_failures=unresolved,
        blocked_tasks=blocked,
        suggested_actions=sorted(set(actions)),
        warnings=warnings,
    )


def _staging_instructions(project: ProjectModel, staging_branch: str) -> list[str]:
    branch = staging_branch or project.staging_branch
    main = project.main_branch or "main"
    return [
        f"Review integrated work: git log {main}..{branch}",
        f"Review the diff: git diff {main}..{branch}",
        "DevSandbox never merges into the main branch automatically.",
        f"Merge manually when satisfied: git checkout {main} && git merge --no-ff {branch}",
    ]


async def _load_state(
    session: AsyncSession, project_id: str
) -> tuple[
    ProjectModel,
    list[TaskModel],
    dict[str, list[AttemptModel]],
    list[IntegrationModel],
    list[EventModel],
]:
    project = await ProjectRepo(session).get(project_id)
    if project is None:
        raise ProjectNotFoundError(f"project {project_id} not found")
    task_repo = TaskRepo(session)
    tasks = await task_repo.list_for_project(project_id, plan_id=project.active_plan_id)
    if not tasks:
        tasks = await task_repo.list_for_project(project_id)
    attempts: dict[str, list[AttemptModel]] = {}
    for task in tasks:
        attempts[task.id] = await AttemptRepo(session).list_for_task(task.id)
    integrations = await IntegrationRepo(session).list_for_project(project_id)
    result = await session.execute(
        select(EventModel)
        .where(EventModel.project_id == project_id)
        .order_by(EventModel.at, EventModel.id)
    )
    events = list(result.scalars().all())
    return project, tasks, attempts, integrations, events


async def _load_dependencies(session: AsyncSession, tasks: list[TaskModel]) -> dict[str, list[str]]:
    repo = TaskRepo(session)
    return {task.id: await repo.dependencies_for_task(task.id) for task in tasks}


async def build_report(
    project_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: DevSandboxSettings | None = None,
    git: GitManager | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> ProjectReport:
    """Build a deterministic, redacted report from persisted project state."""
    settings = settings or get_settings()
    async with session_factory() as session:
        project, tasks, attempts_by_task, integrations, events = await _load_state(
            session, project_id
        )
        dependencies = await _load_dependencies(session, tasks)
        plan = (
            await PlanRepo(session).get(project.active_plan_id) if project.active_plan_id else None
        )
        costs = CostRepo(session)
        infra = await costs.total_cost(project_id, "infra")
        inference = await costs.total_cost(project_id, "inference")
        tokens, requests = await costs.usage_totals(project_id)
        spec_hash = project.spec_hash

    task_reports = [
        _task_report(
            task,
            attempts=attempts_by_task.get(task.id, []),
            dependencies=dependencies.get(task.id, []),
        )
        for task in tasks
    ]
    counts = _task_counts(tasks)
    test_results = _test_aggregates(task_reports)
    warnings = _warnings(events, task_reports)
    outstanding = _outstanding(task_reports, warnings)

    staging_head: str | None = None
    if git is not None:
        try:
            staging_head = await git.staging_head(repo_handle_for_project(project))
        except Exception:
            staging_head = None
    if staging_head is None:
        for integration in reversed(integrations):
            if integration.staging_commit_after:
                staging_head = integration.staging_commit_after
                break

    started_at = project.created_at
    finished_at = (
        project.updated_at
        if ProjectStatus(project.status)
        in {
            ProjectStatus.COMPLETED,
            ProjectStatus.FAILED,
            ProjectStatus.CANCELLED,
        }
        else None
    )
    budget = settings.agent.model_budget
    report = ProjectReport(
        header=ReportHeader(
            project_id=project.id,
            name=project.name,
            spec_hash=spec_hash,
            mode=project.mode,
            status=project.status,
            repo_source=_repo_source(project),
            main_branch=project.main_branch,
            base_commit=(plan.source_revision if plan is not None else None),
            staging_branch=project.staging_branch,
            staging_head=staging_head,
            plan_id=project.active_plan_id,
            plan_version=plan.version if plan is not None else None,
            started_at=started_at,
            finished_at=finished_at,
            generated_at=clock(),
        ),
        counts=counts,
        tasks=task_reports,
        integrations=[_integration_report(row) for row in integrations],
        test_results=test_results,
        costs=ReportCosts(
            infra_usd=round(infra, 4),
            inference_usd=round(inference, 4),
            total_usd=round(infra + inference, 4),
            tokens=tokens,
            requests=requests,
            budget_tokens_remaining=(
                None if budget.max_tokens is None else max(0, budget.max_tokens - tokens)
            ),
            budget_requests_remaining=(
                None if budget.max_requests is None else max(0, budget.max_requests - requests)
            ),
        ),
        resource_events=_resource_events(events, task_reports),
        outstanding=outstanding,
        staging_instructions=_staging_instructions(project, project.staging_branch),
        warnings=warnings,
    )
    # Redaction is applied to the whole serialized report so no field added in
    # the future can accidentally bypass it.
    safe = redact(report.model_dump(mode="json"))
    return ProjectReport.model_validate(safe)


__all__ = ["build_report"]


def _repo_source_json(project: ProjectModel) -> RepoSource | None:
    """Return a parsed repo source when available (used by tests/CLI helpers)."""
    raw = _json_dict(project.repo_source_json)
    if not raw:
        return None
    try:
        return RepoSource.model_validate(raw)
    except ValueError:
        return None
