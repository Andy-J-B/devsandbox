"""Integration tests for the plan lifecycle service (plan 06)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.fakes.planner import FakePlanner
from tests.planner_helpers import make_draft, make_spec

from devsandbox.common.errors import (
    PlanLimitError,
    PlanStateError,
    ProjectNotFoundError,
)
from devsandbox.common.ids import TaskId
from devsandbox.common.types import CheckSpec, RepoSource, TaskStatus, TokenUsage
from devsandbox.config.policy import SecurityPolicy
from devsandbox.db.models import CostEntryModel, ProjectModel, TaskModel
from devsandbox.db.repositories.plan_repo import PlanRepo
from devsandbox.db.repositories.task_repo import TaskRepo
from devsandbox.planner.models import Plan, PlanPatch, PlanStatus, ProposedPlan
from devsandbox.planner.service import PlanService


async def _project(
    session,
    *,
    project_id: str = "proj-1",
    mode: str = "supervised",
    commands: list[CheckSpec] | None = None,
    goals: list[str] | None = None,
) -> ProjectModel:
    spec = make_spec(
        goals=goals or ["Add feature A", "Fix bug B"],
        commands=commands
        if commands is not None
        else [CheckSpec(name="tests", run="pytest -q", required=True)],
    )
    project = ProjectModel(
        id=project_id,
        name=spec.name,
        mode=mode,
        status="loaded",
        spec_json=spec.model_dump_json(),
        repo_path="/tmp/devsandbox-test-repo",
        repo_source_json=RepoSource(path="/tmp/devsandbox-test-repo").model_dump_json(),
    )
    session.add(project)
    await session.flush()
    return project


def _service(session, settings, planner: FakePlanner) -> PlanService:
    return PlanService(
        session,
        planner,
        SecurityPolicy(settings),
        settings=settings,
    )


def _proposed(*tasks) -> ProposedPlan:
    return ProposedPlan(tasks=list(tasks), rationale="test plan", generated_by="fake")


async def _task_by_key(session, plan_id: str, key: str) -> TaskModel:
    task = await TaskRepo(session).get_by_key(plan_id, key)
    assert task is not None
    return task


async def test_create_plan_persists_tasks_and_dependencies(db_session, settings) -> None:
    await _project(db_session)
    planner = FakePlanner(
        [
            _proposed(
                make_draft("task-a"),
                make_draft("task-b", depends_on=["task-a"]),
            )
        ]
    )
    service = _service(db_session, settings, planner)
    plan = await service.create_plan("proj-1")

    assert plan.status == PlanStatus.PROPOSED
    assert plan.version == 1
    assert [task.key for task in plan.tasks] == ["task-a", "task-b"]

    models = await TaskRepo(db_session).list_for_plan(plan.id)
    assert len(models) == 2
    by_key = {model.key: model for model in models}
    dependencies = await TaskRepo(db_session).dependencies_for_task(by_key["task-b"].id)
    assert dependencies == [by_key["task-a"].id]


async def test_invalid_plan_is_rejected_and_issues_are_stored(db_session, settings) -> None:
    await _project(db_session, commands=[])
    invalid = make_draft(
        "task-a",
        instructions="",
        success_criteria=[],
        checks=[],
    )
    planner = FakePlanner([_proposed(invalid)])
    service = _service(db_session, settings, planner)
    plan = await service.create_plan("proj-1")

    assert plan.status == PlanStatus.REJECTED
    assert plan.errors
    stored = await PlanRepo(db_session).get(plan.id)
    assert stored is not None
    assert stored.status == "rejected"
    assert stored.issues_json

    with pytest.raises(PlanStateError):
        await service.approve_plan(plan.id, approver="tester")


async def test_project_level_checks_are_injected(db_session, settings) -> None:
    await _project(db_session)
    planner = FakePlanner([_proposed(make_draft("task-a", checks=[]))])
    service = _service(db_session, settings, planner)
    plan = await service.create_plan("proj-1")

    assert plan.status == PlanStatus.PROPOSED
    task = await _task_by_key(db_session, plan.id, "task-a")
    assert task.checks_json is not None
    assert task.checks_json[0]["run"] == "pytest -q"


async def test_optional_only_project_checks_get_a_required_fallback(db_session, settings) -> None:
    await _project(
        db_session,
        commands=[CheckSpec(name="lint", run="ruff check .", required=False)],
    )
    planner = FakePlanner([_proposed(make_draft("task-a", checks=[]))])
    service = _service(db_session, settings, planner)
    plan = await service.create_plan("proj-1")

    assert plan.status == PlanStatus.PROPOSED
    task = await _task_by_key(db_session, plan.id, "task-a")
    assert task.checks_json is not None
    assert any(check["required"] for check in task.checks_json)


async def test_approve_activates_and_supersedes(db_session, settings) -> None:
    project = await _project(db_session)
    planner = FakePlanner(
        [
            _proposed(make_draft("task-a")),
            _proposed(make_draft("task-b")),
        ]
    )
    service = _service(db_session, settings, planner)

    first = await service.create_plan("proj-1")
    first_active = await service.approve_plan(first.id, approver="human")
    assert first_active.status == PlanStatus.ACTIVE
    assert not first_active.errors

    second = await service.create_plan("proj-1")
    second_active = await service.approve_plan(second.id, approver="human")
    assert second_active.status == PlanStatus.ACTIVE

    stored_first = await PlanRepo(db_session).get(first.id)
    assert stored_first is not None
    assert stored_first.status == "superseded"
    assert project.active_plan_id == second.id


async def test_autonomous_mode_auto_approves(db_session, settings) -> None:
    project = await _project(db_session, mode="autonomous")
    planner = FakePlanner([_proposed(make_draft("task-a"))])
    service = _service(db_session, settings, planner)

    plan = await service.create_plan("proj-1")
    assert plan.status == PlanStatus.ACTIVE
    assert plan.approved_by == "autonomous"
    assert project.active_plan_id == plan.id


async def test_edit_plan_revalidates(db_session, settings) -> None:
    await _project(db_session)
    planner = FakePlanner([_proposed(make_draft("task-a"))])
    service = _service(db_session, settings, planner)
    plan = await service.create_plan("proj-1")

    bad = make_draft("task-bad", instructions="", success_criteria=[], checks=[])
    edited = await service.edit_plan(plan.id, PlanPatch(tasks=[bad]))
    assert edited.status == PlanStatus.REJECTED
    assert edited.errors

    good = make_draft("task-good")
    edited = await service.edit_plan(plan.id, PlanPatch(tasks=[good]))
    assert edited.status == PlanStatus.PROPOSED
    assert not edited.errors


async def test_reject_plan(db_session, settings) -> None:
    await _project(db_session)
    planner = FakePlanner([_proposed(make_draft("task-a"))])
    service = _service(db_session, settings, planner)
    plan = await service.create_plan("proj-1")

    rejected = await service.reject_plan(plan.id, reason="not needed")
    assert rejected.status == PlanStatus.REJECTED
    assert rejected.rejection_reason == "not needed"


async def test_replan_preserves_completed_tasks(db_session, settings) -> None:
    await _project(db_session)
    planner = FakePlanner(
        [
            _proposed(
                make_draft("task-a"),
                make_draft("task-b", depends_on=["task-a"]),
            )
        ]
    )
    service = _service(db_session, settings, planner)
    first = await service.create_plan("proj-1")
    await service.approve_plan(first.id, approver="human")

    task_a = await _task_by_key(db_session, first.id, "task-a")
    await TaskRepo(db_session).transition(
        TaskId(task_a.id),
        [TaskStatus.PENDING],
        TaskStatus.COMPLETED,
        "test-complete",
        result_json={"commit": "abc123"},
    )

    planner.queue_replan(
        _proposed(
            make_draft("task-b"),
            make_draft("task-c", depends_on=["task-b"]),
        )
    )
    replanned = await service.replan("proj-1", failures=[])
    assert replanned.version == 2
    assert replanned.generated_by.startswith("replan:")
    assert {task.key for task in replanned.tasks} == {"task-a", "task-b", "task-c"}

    preserved = await _task_by_key(db_session, replanned.id, "task-a")
    assert preserved.status == "completed"
    assert preserved.result_json == {"commit": "abc123"}
    for key in ("task-b", "task-c"):
        pending = await _task_by_key(db_session, replanned.id, key)
        assert pending.status == "pending"


async def test_replan_cannot_widen_policy(db_session, settings) -> None:
    await _project(db_session)
    planner = FakePlanner([_proposed(make_draft("task-a"))])
    service = _service(db_session, settings, planner)
    first = await service.create_plan("proj-1")
    await service.approve_plan(first.id, approver="human")

    hostile = make_draft(
        "task-a",
        timeout_seconds=99999,
        max_retries=99,
    )
    hostile = hostile.model_copy(
        update={
            "resources": hostile.resources.model_copy(
                update={
                    "cpu_cores": 99.0,
                    "memory_mb": 999999,
                    "disk_mb": 999999,
                    "pids_limit": 999999,
                }
            )
        }
    )
    planner.queue_replan(_proposed(hostile))
    replanned = await service.replan("proj-1", failures=[])
    task = await _task_by_key(db_session, replanned.id, "task-a")

    assert task.resource_profile_json is not None
    assert task.resource_profile_json["cpu_cores"] <= SecurityPolicy(settings).MAX_CPU_CORES
    assert task.resource_profile_json["memory_mb"] <= SecurityPolicy(settings).MAX_MEMORY_MB
    assert task.timeout_seconds <= settings.planner.max_task_timeout_seconds
    assert task.max_retries <= settings.retries.default_max_retries


async def test_plan_diff(db_session, settings) -> None:
    old = Plan(
        id="plan-old",
        project_id="proj-1",
        version=1,
        tasks=[make_draft("task-a"), make_draft("task-b", depends_on=["task-a"])],
        rationale="old",
    )
    new = Plan(
        id="plan-new",
        project_id="proj-1",
        version=2,
        tasks=[
            make_draft("task-a", title="Changed title"),
            make_draft("task-c"),
        ],
        rationale="new",
    )
    diff = PlanService.plan_diff(old, new)
    assert diff.added == ["task-c"]
    assert diff.removed == ["task-b"]
    assert diff.changed == ["task-a"]
    assert diff.rationale_changed
    assert diff.version_from == 1
    assert diff.version_to == 2


async def test_autonomous_replan_limit(db_session, settings) -> None:
    await _project(db_session, mode="autonomous")
    planner = FakePlanner([_proposed(make_draft("task-a"))])
    service = _service(db_session, settings, planner)
    await service.create_plan("proj-1")

    await service.replan("proj-1", failures=[])
    await service.replan("proj-1", failures=[])
    with pytest.raises(PlanLimitError):
        await service.replan("proj-1", failures=[])


async def test_planner_token_usage_is_recorded_as_inference_cost(db_session, settings) -> None:
    await _project(db_session)
    planner = FakePlanner([_proposed(make_draft("task-a"))])
    planner.last_token_usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    service = _service(db_session, settings, planner)
    await service.create_plan("proj-1")

    result = await db_session.execute(
        select(CostEntryModel).where(CostEntryModel.project_id == "proj-1")
    )
    entries = list(result.scalars().all())
    assert len(entries) == 1
    assert entries[0].kind == "inference"
    assert entries[0].units_json == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "model": "opencode",
    }


async def test_missing_project_raises(db_session, settings) -> None:
    service = _service(db_session, settings, FakePlanner())
    with pytest.raises(ProjectNotFoundError):
        await service.create_plan("missing")
