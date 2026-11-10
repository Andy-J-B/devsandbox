"""Integration tests for the project lifecycle service."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.fakes.context import FakeContextManager
from tests.fakes.gitmgr import FakeGitManager
from tests.fakes.planner import FakePlanner
from tests.planner_helpers import make_spec

from devsandbox.common.errors import InvalidTransition, PolicyViolation
from devsandbox.common.types import CheckSpec, ProjectMode, ProjectStatus, TaskStatus
from devsandbox.config.policy import SecurityPolicy
from devsandbox.db.repositories import EventRepo, ProjectRepo, TaskRepo
from devsandbox.orchestrator.models import OrchestratorConfig
from devsandbox.orchestrator.project_service import ProjectService
from devsandbox.orchestrator.state_machine import is_legal_project
from devsandbox.validation.models import BaselineReport


def _service(
    maker, settings, *, planner=None, git=None, context=None, baseline_runner=None
) -> ProjectService:
    return ProjectService(
        maker,
        git=git or FakeGitManager(),
        context=context or FakeContextManager(),
        planner=planner or FakePlanner(),
        policy=SecurityPolicy(settings),
        settings=settings,
        config=OrchestratorConfig(),
        baseline_runner=baseline_runner,
    )


async def _project_status(maker, project_id: str = "proj-1") -> ProjectStatus:
    async with maker() as session:
        project = await ProjectRepo(session).get(project_id)
        assert project is not None
        return ProjectStatus(project.status)


async def test_supervised_lifecycle_waits_for_approval(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    service = _service(maker, settings)
    project = await service.submit_project(make_spec(), project_id="proj-1")
    assert ProjectStatus(project.status) is ProjectStatus.LOADED

    profile = await service.analyze("proj-1")
    assert profile.name == "fake-repo"
    assert await _project_status(maker) is ProjectStatus.ANALYZING

    plan = await service.plan("proj-1")
    assert await _project_status(maker) is ProjectStatus.AWAITING_APPROVAL
    assert plan.tasks

    async with maker() as session:
        tasks = await TaskRepo(session).list_for_project("proj-1")
    assert tasks and all(TaskStatus(row.status) is TaskStatus.PENDING for row in tasks)

    approved = await service.approve_plan("proj-1", plan.id, approver="user")
    assert approved.status.value == "active"
    assert await _project_status(maker) is ProjectStatus.EXECUTING

    async with maker() as session:
        stored = await ProjectRepo(session).get("proj-1")
    assert stored is not None and stored.active_plan_id == plan.id


async def test_analyze_baselines_the_project_validation_checks(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    captured: list[list[CheckSpec]] = []

    async def baseline(repo, revision: str, checks: list[CheckSpec]) -> BaselineReport:
        captured.append(checks)
        return BaselineReport(status="passed", revision=revision)

    service = _service(maker, settings, baseline_runner=baseline)
    spec = make_spec(commands=[CheckSpec(name="tests", run="pytest -q", required=True)])
    await service.submit_project(spec, project_id="proj-1")
    await service.analyze("proj-1")

    assert len(captured) == 1
    assert [check.name for check in captured[0]] == ["tests"]


async def test_autonomous_lifecycle_auto_approves(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    service = _service(maker, settings)
    spec = make_spec().model_copy(update={"mode": ProjectMode.AUTONOMOUS})
    await service.submit_project(spec, project_id="proj-1")
    await service.analyze("proj-1")
    plan = await service.plan("proj-1")

    assert plan.status.value == "active"
    assert await _project_status(maker) is ProjectStatus.EXECUTING


async def test_mode_downgrade_allowed_upgrade_requires_user(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    service = _service(maker, settings)
    spec = make_spec().model_copy(update={"mode": ProjectMode.AUTONOMOUS})
    await service.submit_project(spec, project_id="proj-1")

    downgraded = await service.set_mode("proj-1", ProjectMode.SUPERVISED)
    assert downgraded.mode == ProjectMode.SUPERVISED.value

    with pytest.raises(PolicyViolation):
        await service.set_mode("proj-1", ProjectMode.AUTONOMOUS, requested_by="planner")
    upgraded = await service.set_mode("proj-1", ProjectMode.AUTONOMOUS, requested_by="user")
    assert upgraded.mode == ProjectMode.AUTONOMOUS.value


async def test_pause_resume_and_cancel(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    service = _service(maker, settings)
    spec = make_spec().model_copy(update={"mode": ProjectMode.AUTONOMOUS})
    await service.submit_project(spec, project_id="proj-1")
    await service.analyze("proj-1")
    await service.plan("proj-1")

    paused = await service.pause("proj-1")
    assert ProjectStatus(paused.status) is ProjectStatus.PAUSED
    # Pausing twice is idempotent and does not fail.
    assert ProjectStatus((await service.pause("proj-1")).status) is ProjectStatus.PAUSED

    resumed = await service.resume("proj-1")
    assert ProjectStatus(resumed.status) is ProjectStatus.EXECUTING
    # Resuming an executing project is a no-op (no spurious resumed event).
    await service.resume("proj-1")

    cancelled = await service.cancel("proj-1")
    assert ProjectStatus(cancelled.status) is ProjectStatus.CANCELLED
    async with maker() as session:
        tasks = await TaskRepo(session).list_for_project("proj-1")
        events = await EventRepo(session).list_for_project("proj-1", limit=200)
    assert all(TaskStatus(row.status) is TaskStatus.CANCELLED for row in tasks)
    assert len([event for event in events if event.type == "project.resumed"]) == 1


async def test_illegal_lifecycle_transition_is_rejected(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    service = _service(maker, settings)
    await service.submit_project(make_spec(), project_id="proj-1")
    with pytest.raises(InvalidTransition):
        await service.start("proj-1")

    async with maker() as session:
        from devsandbox.db.repositories import ProjectRepo

        with pytest.raises(InvalidTransition):
            await ProjectRepo(session, is_legal_project).transition(
                "proj-1", [ProjectStatus.LOADED], ProjectStatus.EXECUTING, "illegal"
            )
