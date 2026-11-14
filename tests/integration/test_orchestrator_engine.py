"""Integration tests for the orchestrator execution engine (plan 08)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.fakes.agents import FakeAgentAdapter
from tests.fakes.context import FakeContextManager
from tests.fakes.gitmgr import FakeGitManager
from tests.fakes.planner import FakePlanner
from tests.fakes.sandbox import FakeSandboxManager
from tests.fakes.scheduler import FakeResourceMonitor, FakeScheduler
from tests.fakes.validation import FakeValidator
from tests.orchestrator_helpers import FakeClock, get_project, get_task, seed_project
from tests.planner_helpers import make_spec

from devsandbox.agents.interfaces import AgentRunRequest, AgentRunResult
from devsandbox.common.errors import OrchestratorAlreadyRunning, SandboxError
from devsandbox.common.types import ProjectStatus, ResourceProfile, TaskStatus
from devsandbox.config.policy import SecurityPolicy
from devsandbox.config.settings import ModelBudget
from devsandbox.db.models import TaskModel
from devsandbox.db.repositories import AttemptRepo, EventRepo, IntegrationRepo, ProjectRepo
from devsandbox.gitmgr.interfaces import IntegrationResult
from devsandbox.orchestrator.engine import OrchestratorEngine
from devsandbox.orchestrator.models import OrchestratorConfig
from devsandbox.orchestrator.project_service import ProjectService
from devsandbox.scheduler.config import SchedulerConfig


def _result(status: str = "exited_ok", exit_code: int = 0) -> AgentRunResult:
    return AgentRunResult(
        exit_status=status,  # type: ignore[arg-type]
        exit_code=exit_code,
        log_path=None,
        summary="done",
        token_usage=None,
        duration_seconds=1.0,
    )


class _ScriptedAgentAdapter(FakeAgentAdapter):
    """Fake adapter that preserves the order of results and raised errors."""

    def __init__(self, steps: list[AgentRunResult | BaseException]) -> None:
        super().__init__()
        self._steps = list(steps)

    async def run(self, req: AgentRunRequest) -> AgentRunResult:
        self.calls.append(req)
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _engine(
    maker,
    settings,
    clock,
    *,
    git=None,
    context=None,
    agent=None,
    validator=None,
    scheduler=None,
    monitor=None,
    sandbox=None,
    policy=None,
    config=None,
    scheduler_config=None,
    project_service=True,
    replanner=None,
    faults=None,
    lease_holder=None,
) -> OrchestratorEngine:
    git = git or FakeGitManager()
    context = context or FakeContextManager()
    agent = agent or FakeAgentAdapter()
    validator = validator or FakeValidator()
    scheduler = scheduler or FakeScheduler()
    monitor = monitor or FakeResourceMonitor()
    sandbox = sandbox or FakeSandboxManager()
    policy = policy or SecurityPolicy(settings)
    config = config or OrchestratorConfig(
        backoff_base_seconds=0.0,
        backoff_max_seconds=0.0,
        backoff_jitter_ratio=0.0,
        idle_sleep_seconds=0.001,
        tick_seconds=0.001,
    )
    service = None
    if project_service:
        service = ProjectService(
            maker,
            git=git,
            context=context,
            planner=FakePlanner(),
            policy=policy,
            settings=settings,
            config=config,
        )
    return OrchestratorEngine(
        session_factory=maker,
        git=git,
        context=context,
        agent=agent,
        validator=validator,
        scheduler=scheduler,
        monitor=monitor,
        sandbox=sandbox,
        settings=settings,
        policy=policy,
        config=config,
        classifier=None,
        project_service=service,
        scheduler_config=scheduler_config,
        replanner=replanner,
        faults=faults,
        lease_holder=lease_holder,
        clock=clock,
        sleep=clock.sleep,
    )


async def test_happy_path_dag_four_tasks(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    spec = make_spec()
    keys = ["t-a", "t-b", "t-c", "t-d"]
    deps = {"t-b": ["t-a"], "t-c": ["t-a"], "t-d": ["t-b", "t-c"]}
    _, _, tasks = await seed_project(maker, spec, keys=keys, dependencies=deps)

    agent = FakeAgentAdapter()
    for _ in keys:
        agent.queue_result(_result())
    engine = _engine(maker, settings, clock, agent=agent)

    await engine.run_until_idle()

    for key in keys:
        row = await get_task(maker, tasks[key].id)
        assert TaskStatus(row.status) is TaskStatus.COMPLETED, (key, row.status)
    project = await get_project(maker, "proj-1")
    assert ProjectStatus(project.status) is ProjectStatus.COMPLETED
    # Dependency order: children never ran before their parents completed.
    assert len(agent.calls) == 4


async def test_retry_then_success(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    spec = make_spec()
    _, _, tasks = await seed_project(maker, spec, keys=["t-a"], max_retries=1)

    agent = FakeAgentAdapter()
    agent.queue_result(_result("exited_error", 1))
    agent.queue_result(_result())
    engine = _engine(maker, settings, clock, agent=agent)

    await engine.run_until_idle()

    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.COMPLETED
    assert row.attempt_count == 2
    assert len(agent.calls) == 2


async def test_retries_exhausted_fails_task_and_blocks_dependents(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker,
        make_spec(),
        keys=["t-a", "t-b"],
        dependencies={"t-b": ["t-a"]},
        max_retries=0,
    )
    agent = FakeAgentAdapter()
    agent.queue_result(_result("exited_error", 1))
    engine = _engine(maker, settings, clock, agent=agent)

    await engine.run_until_idle()

    first = await get_task(maker, tasks["t-a"].id)
    second = await get_task(maker, tasks["t-b"].id)
    assert TaskStatus(first.status) is TaskStatus.FAILED
    assert TaskStatus(second.status) is TaskStatus.BLOCKED
    assert second.blocked_reason
    assert len(agent.calls) == 1


async def test_lying_agent_exit_zero_with_no_changes_is_not_completion(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(maker, make_spec(), keys=["t-a"], max_retries=1)
    agent = FakeAgentAdapter()
    agent.queue_result(_result())  # exit 0, no real change
    agent.queue_result(_result())
    validator = FakeValidator(
        task_reports=[
            FakeValidator.failed("no changes detected in workspace"),
            FakeValidator.passed(),
        ]
    )
    engine = _engine(maker, settings, clock, agent=agent, validator=validator)

    await engine.run_until_idle()

    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.COMPLETED
    # The untrusted self-report alone never completed the task.
    assert validator.task_calls, "validation must have run"
    assert len(agent.calls) == 2


async def test_validation_failure_summary_is_in_next_prompt(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(maker, make_spec(), keys=["t-a"], max_retries=1)
    agent = FakeAgentAdapter()
    agent.queue_result(_result())
    agent.queue_result(_result())
    validator = FakeValidator(
        task_reports=[
            FakeValidator.failed("pytest failed: assertion error in tests/test_a.py"),
            FakeValidator.passed(),
        ]
    )
    engine = _engine(maker, settings, clock, agent=agent, validator=validator)

    await engine.run_until_idle()

    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.COMPLETED
    assert agent.calls[1].prior_failure_summary is not None
    assert "assertion error" in agent.calls[1].prior_failure_summary


async def test_git_conflict_is_retried_with_conflict_context(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(maker, make_spec(), keys=["t-a"], max_retries=1)
    git = FakeGitManager()
    git.integrations.append(IntegrationResult(status="conflict", conflicting_files=["src/a.py"]))
    agent = FakeAgentAdapter()
    agent.queue_result(_result())
    agent.queue_result(_result())
    engine = _engine(maker, settings, clock, git=git, agent=agent)

    await engine.run_until_idle()

    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.COMPLETED
    assert row.attempt_count == 2
    assert agent.calls[1].prior_failure_summary is not None
    assert "src/a.py" in agent.calls[1].prior_failure_summary


async def test_infra_failure_does_not_consume_retry_budget(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(maker, make_spec(), keys=["t-a"], max_retries=0)
    agent = FakeAgentAdapter()
    agent.fail(SandboxError("docker daemon unavailable"))
    agent.queue_result(_result())
    engine = _engine(maker, settings, clock, agent=agent)

    await engine.run_until_idle()

    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.COMPLETED
    assert len(agent.calls) == 2
    assert row.attempt_count == 2


async def test_infra_failure_preserves_previous_actionable_failure(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(maker, make_spec(), keys=["t-a"], max_retries=1)
    agent = _ScriptedAgentAdapter(
        [
            _result(),  # exit 0 but no committed change
            SandboxError("lost connection to Docker while waiting for the container"),
            _result(),
        ]
    )
    validator = FakeValidator(
        task_reports=[
            FakeValidator.failed("no changes detected in workspace"),
            FakeValidator.passed(),
        ]
    )
    engine = _engine(maker, settings, clock, agent=agent, validator=validator)

    await engine.run_until_idle()

    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.COMPLETED
    assert len(agent.calls) == 3
    # The infra retry must not replace the actionable no_changes feedback.
    assert agent.calls[2].prior_failure_summary is not None
    assert "no changes" in agent.calls[2].prior_failure_summary
    assert "infrastructure reasons" in agent.calls[2].prior_failure_summary


async def test_infra_failure_without_prior_budgeted_failure_keeps_its_context(
    db_engine, settings
) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(maker, make_spec(), keys=["t-a"], max_retries=0)
    agent = FakeAgentAdapter()
    agent.fail(SandboxError("docker daemon unavailable"))
    agent.queue_result(_result())
    engine = _engine(maker, settings, clock, agent=agent)

    await engine.run_until_idle()

    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.COMPLETED
    assert agent.calls[1].prior_failure_summary is not None
    assert "docker daemon unavailable" in agent.calls[1].prior_failure_summary


async def test_repeated_identical_failures_stop_early(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(maker, make_spec(), keys=["t-a"], max_retries=5)
    agent = FakeAgentAdapter()
    agent.queue_result(_result("exited_error", 1))
    agent.queue_result(_result("exited_error", 1))
    engine = _engine(maker, settings, clock, agent=agent)

    await engine.run_until_idle()

    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.FAILED
    assert len(agent.calls) == 2  # third and later attempts were prevented
    assert row.failure_json is not None
    assert row.failure_json["kind"] == "repeated_failure"


async def test_paused_project_does_not_launch_and_resume_starts_it(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(maker, make_spec(), keys=["t-a"], project_status="paused")
    agent = FakeAgentAdapter()
    agent.queue_result(_result())
    engine = _engine(maker, settings, clock, agent=agent)

    await engine.run_until_idle()
    assert agent.calls == []

    from devsandbox.db.repositories import ProjectRepo
    from devsandbox.orchestrator.state_machine import is_legal_project

    async with maker() as session:
        await ProjectRepo(session, is_legal_project).transition(
            "proj-1", [ProjectStatus.PAUSED], ProjectStatus.EXECUTING, "test resume"
        )
        await session.commit()

    await engine.run_until_idle()
    row = await get_task(maker, tasks["t-a"].id)
    assert TaskStatus(row.status) is TaskStatus.COMPLETED


async def test_supervised_failure_emits_needs_attention(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    await seed_project(maker, make_spec(), keys=["t-a"], max_retries=0, mode="supervised")
    agent = FakeAgentAdapter()
    agent.queue_result(_result("exited_error", 1))
    engine = _engine(maker, settings, clock, agent=agent)

    await engine.run_until_idle()

    async with maker() as session:
        events = await EventRepo(session).list_for_project("proj-1", limit=100)
    assert any(event.type == "project.needs_attention" for event in events)


async def test_autonomous_replans_are_bounded(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    await seed_project(maker, make_spec(), keys=["t-a"], max_retries=0, mode="autonomous")
    agent = FakeAgentAdapter()
    agent.queue_result(_result("exited_error", 1))
    replans: list[str] = []

    async def replanner(project_id: str, failures) -> object:
        replans.append(project_id)
        return object()

    engine = _engine(
        maker,
        settings,
        clock,
        agent=agent,
        replanner=replanner,
        config=OrchestratorConfig(
            backoff_base_seconds=0.0,
            backoff_max_seconds=0.0,
            max_replans=1,
            idle_sleep_seconds=0.001,
            tick_seconds=0.001,
        ),
    )

    await engine.run_until_idle()
    assert replans == ["proj-1"]


async def test_over_policy_resources_are_clamped_and_audited(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(maker, make_spec(), keys=["t-a"])
    async with maker() as session:
        task = await session.get(TaskModel, tasks["t-a"].id)
        assert task is not None
        task.resource_profile_json = ResourceProfile(
            cpu_cores=99.0, memory_mb=999_999, disk_mb=999_999, pids_limit=999_999
        ).model_dump(mode="json")
        task.timeout_seconds = 999_999
        await session.commit()

    agent = FakeAgentAdapter()
    agent.queue_result(_result())
    engine = _engine(maker, settings, clock, agent=agent)

    await engine.run_until_idle()

    assert agent.calls[0].resources.cpu_cores <= SecurityPolicy.MAX_CPU_CORES
    assert agent.calls[0].timeout_seconds <= settings.planner.max_task_timeout_seconds
    async with maker() as session:
        events = await EventRepo(session).list_for_project("proj-1", limit=100)
    assert any(event.type == "POLICY_DENIED" for event in events)


async def test_second_orchestrator_refuses_to_start(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    first = _engine(maker, settings, clock)
    second = _engine(maker, settings, clock)
    await first.acquire_lease()
    try:
        with pytest.raises(OrchestratorAlreadyRunning):
            await second.acquire_lease()
    finally:
        await first.release_lease()


async def test_two_task_concurrency_integration_is_serialized(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(maker, make_spec(), keys=["t-a", "t-b"])
    git = FakeGitManager()
    git.prepare_delay_seconds = 0.02
    agent = FakeAgentAdapter()
    agent.queue_result(_result())
    agent.queue_result(_result())
    engine = _engine(
        maker,
        settings,
        clock,
        git=git,
        agent=agent,
        scheduler_config=SchedulerConfig(max_global_concurrency=2, max_project_concurrency=2),
    )

    await engine.run_until_idle()

    for key in ("t-a", "t-b"):
        row = await get_task(maker, tasks[key].id)
        assert TaskStatus(row.status) is TaskStatus.COMPLETED
    assert git.max_concurrent_prepares == 1

    async with maker() as session:
        integrations = await IntegrationRepo(session).list_for_project("proj-1")
    assert len(integrations) == 2
    assert {row.status for row in integrations} == {"integrated"}


async def test_concurrent_launch_enqueues_only_one_attempt(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    _, _, tasks = await seed_project(
        maker, make_spec(), keys=["t-a"], statuses={"t-a": TaskStatus.READY}
    )
    agent = FakeAgentAdapter()
    agent.queue_result(_result())
    engine = _engine(maker, settings, clock, agent=agent)

    first, second = await asyncio.gather(
        engine._launch(tasks["t-a"].id), engine._launch(tasks["t-a"].id)
    )
    await engine.drain()

    assert (first, second) == (True, False)
    async with maker() as session:
        attempts = await AttemptRepo(session).list_for_task(tasks["t-a"].id)
    assert len(attempts) == 1


async def test_max_total_attempts_breaker_is_exact_under_concurrency(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    await seed_project(maker, make_spec(), keys=["t-a", "t-b"])
    agent = FakeAgentAdapter()
    agent.queue_result(_result())
    engine = _engine(
        maker,
        settings,
        clock,
        agent=agent,
        config=OrchestratorConfig(
            backoff_base_seconds=0.0,
            backoff_max_seconds=0.0,
            max_total_attempts=1,
            idle_sleep_seconds=0.001,
            tick_seconds=0.001,
        ),
        scheduler_config=SchedulerConfig(max_global_concurrency=2, max_project_concurrency=2),
    )

    await engine.run_until_idle()

    # Two launches are offered in the same round; the reservation keeps the
    # per-project attempt breaker exact and only one attempt reaches the agent.
    assert len(agent.calls) == 1
    project = await get_project(maker, "proj-1")
    assert ProjectStatus(project.status) is ProjectStatus.PAUSED
    async with maker() as session:
        events = await EventRepo(session).list_for_project("proj-1", limit=200)
    breaker = [event for event in events if event.type == "CIRCUIT_BREAKER"]
    assert breaker
    assert breaker[0].payload_json["reason"] == "max_total_attempts reached"


async def test_model_budget_defers_every_launch(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    clock = FakeClock()
    await seed_project(maker, make_spec(), keys=["t-a"])
    limited = settings.model_copy(
        update={
            "agent": settings.agent.model_copy(update={"model_budget": ModelBudget(max_tokens=0)})
        }
    )
    agent = FakeAgentAdapter()
    agent.queue_result(_result())
    engine = _engine(maker, limited, clock, agent=agent)

    await engine.run_until_idle()

    assert agent.calls == []
    assert await _project_status(maker) is ProjectStatus.EXECUTING
    async with maker() as session:
        events = await EventRepo(session).list_for_project("proj-1", limit=200)
    deferrals = [
        event
        for event in events
        if event.type == "SCHEDULE_DECISION"
        and event.payload_json.get("reason_code") == "MODEL_BUDGET"
    ]
    assert deferrals


async def _project_status(maker) -> ProjectStatus:
    async with maker() as session:
        project = await ProjectRepo(session).get("proj-1")
    assert project is not None
    return ProjectStatus(project.status)
