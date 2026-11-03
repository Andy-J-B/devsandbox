"""Helpers shared by orchestrator engine and recovery tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from devsandbox.common.types import CheckSpec, TaskStatus
from devsandbox.db.models import (
    AttemptModel,
    PlanModel,
    ProjectModel,
    TaskDependencyModel,
    TaskModel,
)
from devsandbox.spec.models import ProjectSpec


class FakeClock:
    """Deterministic clock whose ``sleep`` advances time."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=max(0.0, seconds))

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def seed_project(
    maker: async_sessionmaker,
    spec: ProjectSpec,
    *,
    project_id: str = "proj-1",
    plan_id: str = "plan-1",
    keys: list[str] | None = None,
    dependencies: dict[str, list[str]] | None = None,
    statuses: dict[str, TaskStatus] | None = None,
    mode: str = "autonomous",
    project_status: str = "executing",
    max_retries: int = 1,
    checks: list[CheckSpec] | None = None,
) -> tuple[ProjectModel, PlanModel, dict[str, TaskModel]]:
    """Persist a project/plan/task graph and return the rows."""
    keys = keys or ["t-a"]
    dependencies = dependencies or {}
    statuses = statuses or {}
    async with maker() as session:
        project = ProjectModel(
            id=project_id,
            name=spec.name,
            mode=mode,
            status=project_status,
            spec_json=spec.model_dump_json(),
            spec_yaml=None,
            repo_source_json=spec.repo.model_dump_json(),
            repo_path=spec.repo.path,
            main_branch=spec.repo.ref,
            staging_branch=f"devsandbox/{project_id}/staging",
            active_plan_id=plan_id,
        )
        session.add(project)
        plan = PlanModel(id=plan_id, project_id=project_id, version=1, status="active")
        session.add(plan)
        tasks: dict[str, TaskModel] = {}
        for index, key in enumerate(keys):
            task = TaskModel(
                id=f"{plan_id}-{key}",
                project_id=project_id,
                plan_id=plan_id,
                key=key,
                title=key,
                description=f"task {key}",
                instructions=f"Implement {key}",
                success_criteria_json=["done"],
                checks_json=[
                    c.model_dump(mode="json")
                    for c in (
                        checks if checks is not None else [CheckSpec(name="tests", run="pytest -q")]
                    )
                ],
                resource_profile_json={},
                timeout_seconds=600,
                max_retries=max_retries,
                status=(statuses.get(key) or TaskStatus.PENDING).value,
                priority=50 - index,
            )
            session.add(task)
            tasks[key] = task
        await session.flush()
        for key, deps in dependencies.items():
            for dep in deps:
                session.add(
                    TaskDependencyModel(
                        task_id=tasks[key].id,
                        depends_on_task_id=tasks[dep].id,
                    )
                )
        await session.commit()
        return project, plan, tasks


async def seed_attempt(
    maker: async_sessionmaker,
    task_id: str,
    *,
    attempt_id: str = "att-1",
    status: str = "running",
    workspace_path: str | None = "/tmp/fake/ws-att-1",
    base_commit: str | None = "base-commit",
    head_commit: str | None = None,
    container_id: str | None = None,
    validation_report: dict | None = None,
    number: int = 1,
    started_at: datetime | None = None,
) -> AttemptModel:
    """Persist an attempt row and point the task at it."""
    async with maker() as session:
        attempt = AttemptModel(
            id=attempt_id,
            task_id=task_id,
            number=number,
            status=status,
            workspace_path=workspace_path,
            base_commit=base_commit,
            head_commit=head_commit,
            container_id=container_id,
            validation_report_json=validation_report,
            branch=f"devsandbox/{task_id}/a{number}",
            started_at=started_at or datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        )
        session.add(attempt)
        task = await session.get(TaskModel, task_id)
        assert task is not None
        task.current_attempt_id = attempt_id
        task.attempt_count = max(task.attempt_count, number)
        await session.commit()
        return attempt


async def get_task(maker: async_sessionmaker, task_id: str) -> TaskModel:
    """Load one task row in a fresh session."""
    from sqlalchemy import select

    async with maker() as session:
        result = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        return result.scalar_one()


async def get_project(maker: async_sessionmaker, project_id: str) -> ProjectModel:
    """Load one project row in a fresh session."""
    from sqlalchemy import select

    async with maker() as session:
        result = await session.execute(select(ProjectModel).where(ProjectModel.id == project_id))
        return result.scalar_one()


__all__ = ["FakeClock", "get_project", "get_task", "seed_attempt", "seed_project"]
