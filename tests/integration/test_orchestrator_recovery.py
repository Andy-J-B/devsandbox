"""Crash-recovery tests covering every row of the plan-08 decision table."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.fakes.agents import FakeAgentAdapter
from tests.fakes.context import FakeContextManager
from tests.fakes.gitmgr import FakeGitManager
from tests.fakes.planner import FakePlanner
from tests.fakes.sandbox import FakeSandboxManager
from tests.fakes.scheduler import FakeResourceMonitor, FakeScheduler
from tests.fakes.validation import FakeValidator
from tests.orchestrator_helpers import (
    FakeClock,
    get_task,
    seed_attempt,
    seed_project,
)
from tests.planner_helpers import make_spec

from devsandbox.agents.interfaces import AgentRunResult
from devsandbox.common.types import TaskStatus
from devsandbox.config.policy import SecurityPolicy
from devsandbox.db.models import IntegrationModel
from devsandbox.db.repositories import AttemptRepo, IntegrationRepo
from devsandbox.orchestrator.engine import FaultInjector, OrchestratorEngine, SimulatedCrash
from devsandbox.orchestrator.models import OrchestratorConfig, RecoveryActionKind
from devsandbox.orchestrator.project_service import ProjectService
from devsandbox.orchestrator.recovery import RecoveryService
from devsandbox.sandbox.interfaces import ManagedContainer


def _result(status: str = "exited_ok", exit_code: int = 0) -> AgentRunResult:
    return AgentRunResult(
        exit_status=status,  # type: ignore[arg-type]
        exit_code=exit_code,
        log_path=None,
        summary="done",
        token_usage=None,
        duration_seconds=1.0,
    )


def _config() -> OrchestratorConfig:
    return OrchestratorConfig(
        backoff_base_seconds=0.0,
        backoff_max_seconds=0.0,
        backoff_jitter_ratio=0.0,
        idle_sleep_seconds=0.001,
        tick_seconds=0.001,
    )


def _engine(
    maker,
    settings,
    clock,
    *,
    git,
    context,
    agent,
    validator,
    sandbox,
    faults=None,
    config=None,
) -> OrchestratorEngine:
    config = config or _config()
    policy = SecurityPolicy(settings)
    return OrchestratorEngine(
        session_factory=maker,
        git=git,
        context=context,
        agent=agent,
        validator=validator,
        scheduler=FakeScheduler(),
        monitor=FakeResourceMonitor(),
        sandbox=sandbox,
        settings=settings,
        policy=policy,
        config=config,
        project_service=ProjectService(
            maker,
            git=git,
            context=context,
            planner=FakePlanner(),
            policy=policy,
            settings=settings,
            config=config,
        ),
        recovery=RecoveryService(
            maker,
            git=git,
            sandbox=sandbox,
            config=config,
            clock=clock,
        ),
        faults=faults,
        lease_holder="recovery-test-orchestrator",
        clock=clock,
        sleep=clock.sleep,
    )


async def _recover(maker, settings, clock, *, git, sandbox) -> object:
    service = RecoveryService(
        maker,
        git=git,
        sandbox=sandbox,
        config=_config(),
        clock=clock,
    )
    return await service.recover()


def _managed(attempt_id: str, state: str = "exited") -> ManagedContainer:
    return ManagedContainer(
        container_id=f"ds-{attempt_id}",
        state=state,
        labels={"devsandbox.attempt": attempt_id},
    )


# ---------------------------------------------------------------------------
# Direct decision-table rows
# ---------------------------------------------------------------------------


async def test_row1_scheduled_no_container_reverts_to_ready(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker,
        make_spec(),
        keys=["t-a"],
        statuses={"t-a": TaskStatus.SCHEDULED},
    )
    await seed_attempt(maker, tasks["t-a"].id, status="scheduled", workspace_path=None)

    report = await _recover(
        maker, settings, clock, git=FakeGitManager(), sandbox=FakeSandboxManager()
    )

    assert [a.kind for a in report.actions] == [RecoveryActionKind.REVERT_READY]
    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.READY
    assert row.current_attempt_id is None


async def test_row2_running_container_is_killed_and_retried(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker, make_spec(), keys=["t-a"], statuses={"t-a": TaskStatus.RUNNING}
    )
    await seed_attempt(maker, tasks["t-a"].id, status="running", container_id="ds-att-1")
    sandbox = FakeSandboxManager()
    sandbox.managed.append(_managed("att-1", "running"))

    report = await _recover(maker, settings, clock, git=FakeGitManager(), sandbox=sandbox)

    assert [a.kind for a in report.actions] == [RecoveryActionKind.KILL_AND_RETRY]
    assert "ds-att-1" in sandbox.destroyed
    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.RETRY_WAIT
    async with maker() as session:
        attempt = await AttemptRepo(session).get("att-1")
    assert attempt is not None and attempt.status == "abandoned"


async def test_row3_running_container_exited_continues_collect(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker, make_spec(), keys=["t-a"], statuses={"t-a": TaskStatus.RUNNING}
    )
    await seed_attempt(maker, tasks["t-a"].id, status="running", container_id="ds-att-1")
    sandbox = FakeSandboxManager()
    sandbox.managed.append(_managed("att-1", "exited"))

    report = await _recover(maker, settings, clock, git=FakeGitManager(), sandbox=sandbox)

    assert [a.kind for a in report.actions] == [RecoveryActionKind.RESUME_COLLECT]
    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.RUNNING


async def test_row4_running_workspace_commits_continues_collect(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker, make_spec(), keys=["t-a"], statuses={"t-a": TaskStatus.RUNNING}
    )
    await seed_attempt(maker, tasks["t-a"].id, status="running", head_commit="deadbeef")

    report = await _recover(
        maker, settings, clock, git=FakeGitManager(), sandbox=FakeSandboxManager()
    )

    assert [a.kind for a in report.actions] == [RecoveryActionKind.RESUME_COLLECT]


async def test_row5_running_nothing_recoverable_retries_infra(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker, make_spec(), keys=["t-a"], statuses={"t-a": TaskStatus.RUNNING}
    )
    await seed_attempt(maker, tasks["t-a"].id, status="running")

    report = await _recover(
        maker, settings, clock, git=FakeGitManager(), sandbox=FakeSandboxManager()
    )

    assert [a.kind for a in report.actions] == [RecoveryActionKind.RETRY_INFRA]
    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.RETRY_WAIT
    assert row.retry_not_before == clock.now


async def test_row6_validating_persisted_report_continues(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker, make_spec(), keys=["t-a"], statuses={"t-a": TaskStatus.VALIDATING}
    )
    await seed_attempt(
        maker,
        tasks["t-a"].id,
        status="validating",
        validation_report={"status": "passed", "checks": [], "summary": "ok"},
    )

    report = await _recover(
        maker, settings, clock, git=FakeGitManager(), sandbox=FakeSandboxManager()
    )

    assert [a.kind for a in report.actions] == [RecoveryActionKind.RESUME_VALIDATION_DECISION]


async def test_row7_validating_no_report_reruns_validation(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker, make_spec(), keys=["t-a"], statuses={"t-a": TaskStatus.VALIDATING}
    )
    await seed_attempt(maker, tasks["t-a"].id, status="validating")

    report = await _recover(
        maker, settings, clock, git=FakeGitManager(), sandbox=FakeSandboxManager()
    )

    assert [a.kind for a in report.actions] == [RecoveryActionKind.RERUN_VALIDATION]


async def test_row8_integrating_prepared_candidate_restarts(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker, make_spec(), keys=["t-a"], statuses={"t-a": TaskStatus.INTEGRATING}
    )
    await seed_attempt(maker, tasks["t-a"].id, status="integrating", head_commit="deadbeef")
    git = FakeGitManager()
    git.staging_heads["proj-1"] = ["before-commit"]
    async with maker() as session:
        session.add(
            IntegrationModel(
                id="int-1",
                project_id="proj-1",
                task_id=tasks["t-a"].id,
                attempt_id="att-1",
                status="prepared",
                staging_commit_before="before-commit",
                staging_commit_after="candidate-commit",
                conflict_files_json=[],
                at=clock.now,
            )
        )
        await session.commit()

    report = await _recover(maker, settings, clock, git=git, sandbox=FakeSandboxManager())

    assert [a.kind for a in report.actions] == [RecoveryActionKind.RESET_AND_RETRY]
    async with maker() as session:
        row = await IntegrationRepo(session).get("int-1")
    assert row is not None and row.status == "prepared"


async def test_row9_integrating_staging_advanced_marks_completed(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker, make_spec(), keys=["t-a"], statuses={"t-a": TaskStatus.INTEGRATING}
    )
    await seed_attempt(maker, tasks["t-a"].id, status="integrating", head_commit="deadbeef")
    git = FakeGitManager()
    git.staging_heads["proj-1"] = ["candidate-commit"]
    async with maker() as session:
        session.add(
            IntegrationModel(
                id="int-1",
                project_id="proj-1",
                task_id=tasks["t-a"].id,
                attempt_id="att-1",
                status="prepared",
                staging_commit_before="before-commit",
                staging_commit_after="candidate-commit",
                conflict_files_json=[],
                at=clock.now,
            )
        )
        await session.commit()

    report = await _recover(maker, settings, clock, git=git, sandbox=FakeSandboxManager())

    assert [a.kind for a in report.actions] == [RecoveryActionKind.MARK_COMPLETED]
    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.COMPLETED
    async with maker() as session:
        attempt = await AttemptRepo(session).get("att-1")
    assert attempt is not None and attempt.status == "completed"


async def test_row10_staging_advanced_unknown_resets_and_retries(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker, make_spec(), keys=["t-a"], statuses={"t-a": TaskStatus.INTEGRATING}
    )
    await seed_attempt(maker, tasks["t-a"].id, status="integrating", head_commit="deadbeef")
    git = FakeGitManager()
    git.staging_heads["proj-1"] = ["someone-else-commit"]
    async with maker() as session:
        session.add(
            IntegrationModel(
                id="int-1",
                project_id="proj-1",
                task_id=tasks["t-a"].id,
                attempt_id="att-1",
                status="prepared",
                staging_commit_before="before-commit",
                staging_commit_after="candidate-commit",
                conflict_files_json=[],
                at=clock.now,
            )
        )
        await session.commit()

    report = await _recover(maker, settings, clock, git=git, sandbox=FakeSandboxManager())

    assert [a.kind for a in report.actions] == [RecoveryActionKind.RESET_AND_RETRY]
    assert git.staging_heads["proj-1"][0] == "before-commit"


# ---------------------------------------------------------------------------
# Crash-injection convergence + idempotency
# ---------------------------------------------------------------------------


async def _crash_then_recover(db_engine, settings, checkpoint: str, *, max_retries=1):
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(maker, make_spec(), keys=["t-a"], max_retries=max_retries)
    git = FakeGitManager()
    context = FakeContextManager()
    sandbox = FakeSandboxManager()
    validator = FakeValidator()
    faults = FaultInjector({checkpoint})
    agent = FakeAgentAdapter()
    agent.queue_result(_result())
    agent.queue_result(_result())

    first = _engine(
        maker,
        settings,
        clock,
        git=git,
        context=context,
        agent=agent,
        validator=validator,
        sandbox=sandbox,
        faults=faults,
    )
    await first.acquire_lease()
    await first.run_once()
    with pytest.raises(SimulatedCrash):
        await first.drain()

    # Simulate a process restart: same lease holder re-acquires; no cleanup ran.
    second = _engine(
        maker,
        settings,
        clock,
        git=git,
        context=context,
        agent=agent,
        validator=validator,
        sandbox=sandbox,
    )
    await second.run_until_idle()
    return maker, tasks, git, validator, agent


@pytest.mark.parametrize(
    "checkpoint",
    [
        "after_attempt_created",
        "after_workspace",
        "before_agent",
        "after_validation",
        "before_integration",
        "after_integration_commit",
    ],
)
async def test_crash_injection_converges(db_engine, settings, checkpoint: str) -> None:
    maker, tasks, git, _validator, _agent = await _crash_then_recover(
        db_engine, settings, checkpoint
    )

    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.COMPLETED, (checkpoint, row.status)
    async with maker() as session:
        integrations = await IntegrationRepo(session).list_for_project("proj-1")
    assert len([row for row in integrations if row.status == "integrated"]) == 1
    commits = [call for call in git.calls if call["op"] == "commit_integration"]
    assert len(commits) == 1


async def test_recovery_is_idempotent(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker, make_spec(), keys=["t-a"], statuses={"t-a": TaskStatus.RUNNING}
    )
    await seed_attempt(maker, tasks["t-a"].id, status="running")
    git = FakeGitManager()
    sandbox = FakeSandboxManager()

    first = await _recover(maker, settings, clock, git=git, sandbox=sandbox)
    second = await _recover(maker, settings, clock, git=git, sandbox=sandbox)

    assert [a.kind for a in first.actions] == [RecoveryActionKind.RETRY_INFRA]
    assert second.actions == []
    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.RETRY_WAIT
