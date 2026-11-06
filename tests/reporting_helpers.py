"""Deterministic report scenarios used by golden-file tests (plan 10)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from devsandbox.db.models import (
    AttemptModel,
    CostEntryModel,
    EventModel,
    IntegrationModel,
    PlanModel,
    ProjectModel,
    TaskDependencyModel,
    TaskModel,
    UsageCounterModel,
)
from devsandbox.spec.models import ProjectSpec

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _check(
    name: str,
    *,
    status: str = "passed",
    failed: int = 0,
    total: int = 10,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "check": name,
        "status": status,
        "passed": status == "passed",
        "required": True,
        "exit_code": 0 if status == "passed" else 1,
        "duration_seconds": 1.5,
        "tests_total": total,
        "tests_failed": failed,
        "tests_skipped": 0,
        "reason": reason,
    }


def _validation(
    *,
    status: str = "passed",
    summary: str = "all checks passed",
    checks: list[dict[str, Any]] | None = None,
    files: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary,
        "checks": checks or [_check("pytest")],
        "files_changed": files or [],
    }


def _project(
    scenario: str,
    *,
    status: str,
    mode: str = "supervised",
    repo: str = "/tmp/demo-repo",
    minutes: int = 90,
) -> ProjectModel:
    return ProjectModel(
        id=f"proj-{scenario}",
        name=f"Report {scenario}",
        mode=mode,
        status=status,
        spec_hash=f"spec-hash-{scenario}",
        spec_json="{}",
        repo_source_json=json.dumps({"path": repo}),
        repo_path=repo,
        main_branch="main",
        staging_branch=f"devsandbox/proj-{scenario}/staging",
        active_plan_id=f"plan-{scenario}",
        created_at=_at(0),
        updated_at=_at(minutes),
    )


def _task(
    scenario: str,
    key: str,
    *,
    status: str,
    created: int,
    updated: int,
    attempt_count: int = 0,
    max_retries: int = 1,
    result: dict[str, Any] | None = None,
    failure: dict[str, Any] | None = None,
    blocked_reason: str | None = None,
) -> TaskModel:
    return TaskModel(
        id=f"task-{scenario}-{key}",
        project_id=f"proj-{scenario}",
        plan_id=f"plan-{scenario}",
        key=key,
        title=f"Task {key}",
        description=f"Description for {key}",
        instructions=f"Implement {key}",
        priority=50,
        status=status,
        success_criteria_json=["works"],
        checks_json=[{"name": "tests", "run": "pytest -q"}],
        expected_files_json=[],
        restricted_files_json=[],
        resource_profile_json={},
        timeout_seconds=600,
        max_retries=max_retries,
        attempt_count=attempt_count,
        result_json=result,
        failure_json=failure,
        blocked_reason=blocked_reason,
        created_at=_at(created),
        updated_at=_at(updated),
    )


def _attempt(
    scenario: str,
    key: str,
    number: int,
    *,
    status: str = "completed",
    exit_status: str = "exited_ok",
    started: int,
    finished: int,
    head_commit: str | None = None,
    validation: dict[str, Any] | None = None,
    failure: dict[str, Any] | None = None,
    log_path: str | None = None,
) -> AttemptModel:
    return AttemptModel(
        id=f"att-{scenario}-{key}-{number}",
        task_id=f"task-{scenario}-{key}",
        number=number,
        status=status,
        workspace_path=f"/tmp/workspaces/{scenario}/{key}/{number}",
        branch=f"devsandbox/proj-{scenario}/{key}/a{number}",
        base_commit=f"base-{scenario}",
        head_commit=head_commit,
        container_id=f"container-{scenario}-{key}-{number}",
        agent_name="opencode",
        started_at=_at(started),
        finished_at=_at(finished),
        exit_status=exit_status,
        agent_log_path=log_path or f"logs/att-{scenario}-{key}-{number}/stdout.log",
        validation_report_json=validation,
        failure_json=failure,
        token_usage_json={"input_tokens": 100, "output_tokens": 50},
        peak_memory_mb=512,
        created_at=_at(started),
        updated_at=_at(finished),
    )


def _integration(
    scenario: str,
    key: str,
    *,
    status: str,
    before: str | None,
    after: str | None,
    detail: str = "",
    conflicts: list[str] | None = None,
    minutes: int = 60,
) -> IntegrationModel:
    return IntegrationModel(
        id=f"int-{scenario}-{key}",
        project_id=f"proj-{scenario}",
        task_id=f"task-{scenario}-{key}",
        attempt_id=f"att-{scenario}-{key}-1",
        status=status,
        staging_commit_before=before,
        staging_commit_after=after,
        conflict_files_json=conflicts or [],
        detail=detail,
        at=_at(minutes),
    )


def _event(
    scenario: str,
    event_id: str,
    event_type: str,
    *,
    minutes: int,
    payload: dict[str, Any] | None = None,
    task_id: str | None = None,
    attempt_id: str | None = None,
) -> EventModel:
    return EventModel(
        id=f"evt-{scenario}-{event_id}",
        project_id=f"proj-{scenario}",
        task_id=task_id,
        attempt_id=attempt_id,
        type=event_type,
        payload_json=payload or {},
        at=_at(minutes),
        created_at=_at(minutes),
        updated_at=_at(minutes),
    )


def _costs(
    scenario: str, *, infra: float, inference: float, tokens: int, requests: int
) -> list[Any]:
    rows: list[Any] = [
        CostEntryModel(
            id=f"cost-{scenario}-infra",
            project_id=f"proj-{scenario}",
            kind="infra",
            amount_usd=infra,
            created_at=_at(1),
            updated_at=_at(1),
        ),
        CostEntryModel(
            id=f"cost-{scenario}-inference",
            project_id=f"proj-{scenario}",
            kind="inference",
            amount_usd=inference,
            created_at=_at(1),
            updated_at=_at(1),
        ),
        UsageCounterModel(
            project_id=f"proj-{scenario}",
            provider="test",
            window_start=_at(0),
            tokens=tokens,
            requests=requests,
            created_at=_at(0),
            updated_at=_at(0),
        ),
    ]
    return rows


async def seed_report_scenario(
    maker: async_sessionmaker,
    scenario: str,
    spec: ProjectSpec,
) -> str:
    """Persist one deterministic scenario and return its project id."""
    del spec  # scenarios carry their own fixed identifiers
    project_id = f"proj-{scenario}"
    tasks: list[TaskModel] = []
    attempts: list[AttemptModel] = []
    integrations: list[IntegrationModel] = []
    events: list[EventModel] = []
    dependencies: list[TaskDependencyModel] = []
    costs: list[Any] = []

    if scenario == "all_success":
        project = _project(scenario, status="completed", mode="autonomous")
        tasks = [
            _task(
                scenario,
                "build",
                status="completed",
                created=1,
                updated=20,
                attempt_count=1,
                result={"commits": [{"sha": "c1", "subject": "build the app"}]},
            ),
            _task(
                scenario,
                "test",
                status="completed",
                created=21,
                updated=40,
                attempt_count=1,
                result={
                    "commits": [{"sha": "c2", "subject": "add tests"}],
                    "files_changed": ["tests/test_app.py"],
                },
            ),
        ]
        attempts = [
            _attempt(
                scenario,
                "build",
                1,
                started=2,
                finished=18,
                head_commit="c1",
                validation=_validation(files=["src/app.py"]),
            ),
            _attempt(
                scenario,
                "test",
                1,
                started=22,
                finished=38,
                head_commit="c2",
                validation=_validation(files=["tests/test_app.py"]),
            ),
        ]
        integrations = [
            _integration(scenario, "build", status="integrated", before=None, after="s1"),
            _integration(scenario, "test", status="integrated", before="s1", after="s2"),
        ]
        events = [
            _event(
                scenario,
                "baseline",
                "validation.baseline",
                minutes=1,
                payload={"status": "passed", "summary": "baseline passed"},
            )
        ]
        costs = _costs(scenario, infra=0.25, inference=0.10, tokens=1000, requests=10)

    elif scenario == "mixed_failures":
        project = _project(scenario, status="failed")
        failed_check = _check(
            "pytest", status="failed", failed=2, total=10, reason="2 tests failed"
        )
        tasks = [
            _task(
                scenario,
                "build",
                status="completed",
                created=1,
                updated=20,
                attempt_count=1,
            ),
            _task(
                scenario,
                "test",
                status="failed",
                created=21,
                updated=60,
                attempt_count=2,
                failure={
                    "kind": "validation_failed",
                    "summary": "tests failed after retries",
                    "diagnostics_path": "logs/att-mixed_failures-test-2/stdout.log",
                },
            ),
            _task(
                scenario,
                "flaky",
                status="failed",
                created=21,
                updated=60,
                attempt_count=1,
                failure={"kind": "timeout", "summary": "agent exceeded the time limit"},
            ),
        ]
        attempts = [
            _attempt(
                scenario,
                "build",
                1,
                started=2,
                finished=18,
                head_commit="c1",
                validation=_validation(files=["src/app.py"]),
            ),
            _attempt(
                scenario,
                "test",
                1,
                started=22,
                finished=40,
                head_commit="c2",
                validation=_validation(
                    status="failed",
                    summary="2 tests failed",
                    checks=[failed_check],
                    files=["src/app.py", "tests/test_app.py"],
                ),
            ),
            _attempt(
                scenario,
                "test",
                2,
                status="failed",
                started=41,
                finished=59,
                head_commit="c3",
                validation=_validation(
                    status="failed",
                    summary="2 tests failed",
                    checks=[failed_check],
                ),
            ),
            _attempt(
                scenario,
                "flaky",
                1,
                status="failed",
                exit_status="timeout",
                started=41,
                finished=59,
                validation=_validation(
                    status="error",
                    summary="agent timed out",
                    checks=[
                        _check(
                            "pytest",
                            status="error",
                            failed=0,
                            total=0,
                            reason="not run: agent timed out",
                        )
                    ],
                ),
            ),
        ]
        integrations = [
            _integration(scenario, "build", status="integrated", before=None, after="s1")
        ]
        events = [
            _event(
                scenario,
                "timeout",
                "attempt.timeout",
                minutes=59,
                payload={"reason": "agent exceeded the time limit"},
                task_id=f"task-{scenario}-flaky",
            )
        ]
        costs = _costs(scenario, infra=0.30, inference=0.12, tokens=2000, requests=20)

    elif scenario == "blocked_chain":
        project = _project(scenario, status="failed")
        tasks = [
            _task(
                scenario,
                "base",
                status="failed",
                created=1,
                updated=20,
                attempt_count=1,
                failure={"kind": "validation_failed", "summary": "base checks failed"},
            ),
            _task(
                scenario,
                "next",
                status="blocked",
                created=21,
                updated=25,
                attempt_count=0,
                blocked_reason="dependency base failed",
            ),
            _task(
                scenario,
                "last",
                status="pending",
                created=21,
                updated=25,
                attempt_count=0,
            ),
        ]
        attempts = [
            _attempt(
                scenario,
                "base",
                1,
                started=2,
                finished=18,
                validation=_validation(
                    status="failed",
                    summary="base checks failed",
                    checks=[_check("pytest", status="failed", failed=1, total=5)],
                ),
            )
        ]
        dependencies = [
            TaskDependencyModel(
                task_id=f"task-{scenario}-next",
                depends_on_task_id=f"task-{scenario}-base",
            ),
            TaskDependencyModel(
                task_id=f"task-{scenario}-last",
                depends_on_task_id=f"task-{scenario}-next",
            ),
        ]
        costs = _costs(scenario, infra=0.10, inference=0.05, tokens=500, requests=5)

    elif scenario == "conflict":
        project = _project(scenario, status="failed")
        tasks = [
            _task(
                scenario,
                "merge",
                status="failed",
                created=1,
                updated=30,
                attempt_count=1,
                failure={"kind": "git_conflict", "summary": "conflict in src/app.py"},
            )
        ]
        attempts = [
            _attempt(
                scenario,
                "merge",
                1,
                started=2,
                finished=28,
                head_commit="c1",
                validation=_validation(files=["src/app.py"]),
                failure={
                    "kind": "git_conflict",
                    "summary": "staging moved; integration conflicted",
                },
            )
        ]
        integrations = [
            _integration(
                scenario,
                "merge",
                status="conflict",
                before="s1",
                after=None,
                detail="merge conflict; staging unchanged",
                conflicts=["src/app.py"],
                minutes=29,
            )
        ]
        costs = _costs(scenario, infra=0.05, inference=0.03, tokens=300, requests=3)

    elif scenario == "policy_denial":
        project = _project(scenario, status="failed")
        tasks = [
            _task(
                scenario,
                "guarded",
                status="failed",
                created=1,
                updated=40,
                attempt_count=1,
                failure={
                    "kind": "tampering",
                    "summary": "agent modified a restricted test file",
                },
            )
        ]
        attempts = [
            _attempt(
                scenario,
                "guarded",
                1,
                started=2,
                finished=38,
                head_commit="c1",
                validation=_validation(
                    status="failed",
                    summary="tampering detected",
                    checks=[_check("pytest", status="failed", failed=1, total=4)],
                ),
                failure={
                    "kind": "tampering",
                    "summary": "agent modified tests/test_app.py",
                },
            )
        ]
        events = [
            _event(
                scenario,
                "denied",
                "POLICY_DENIED",
                minutes=20,
                payload={"reason": "main-branch merge is disabled by policy"},
            ),
            _event(
                scenario,
                "baseline",
                "validation.baseline",
                minutes=1,
                payload={"status": "failed", "summary": "baseline pytest failed"},
            ),
        ]
        costs = _costs(scenario, infra=0.02, inference=0.01, tokens=120, requests=2)

    else:  # pragma: no cover - guards a typo in the parametrised test list
        raise AssertionError(f"unknown report scenario: {scenario}")

    plan = PlanModel(
        id=f"plan-{scenario}",
        project_id=project_id,
        version=1,
        status="active",
        generated_by="fake",
        planner_version="1.0",
        source_revision=f"base-{scenario}",
        approved_by="tester",
        approved_at=_at(1),
        rationale="deterministic golden scenario",
        created_at=_at(0),
        updated_at=_at(1),
    )

    async with maker() as session:
        session.add(project)
        session.add(plan)
        session.add_all(tasks)
        await session.flush()
        session.add_all(attempts)
        session.add_all(integrations)
        session.add_all(events)
        session.add_all(dependencies)
        session.add_all(costs)
        await session.commit()
    return project_id


__all__ = ["T0", "seed_report_scenario"]
