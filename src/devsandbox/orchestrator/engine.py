"""Execution engine: readiness, scheduling, attempt pipeline, retries, costs.

A single engine loop runs per process and is guarded by a DB lease.  The
per-attempt pipeline persists intent before every external side effect and is
safe to re-enter after a crash (see :mod:`devsandbox.orchestrator.recovery`).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..agents.interfaces import AgentAdapter, AgentRunRequest, AgentRunResult
from ..agents.models import model_config_from_settings
from ..common.errors import (
    InvalidTransition,
    OrchestratorAlreadyRunning,
)
from ..common.ids import AttemptId, TaskId
from ..common.logging import get_logger
from ..common.time import utcnow
from ..common.types import (
    CheckSpec,
    NetworkPolicy,
    ProjectMode,
    ProjectStatus,
    RepoHandle,
    TaskOutcome,
    TaskOutcomeNote,
    TaskStatus,
)
from ..config.policy import SecurityPolicy
from ..config.settings import DevSandboxSettings, get_settings
from ..context.interfaces import ContextManager
from ..db.models import (
    AttemptModel,
    CostEntryModel,
    IntegrationModel,
    ProjectModel,
    TaskModel,
)
from ..db.repositories import (
    AttemptRepo,
    CostRepo,
    IntegrationRepo,
    LeaseRepo,
    ProjectRepo,
    TaskRepo,
)
from ..gitmgr.interfaces import ChangeSet, GitManager, IntegrationResult, Workspace
from ..planner.models import FailureRecord as PlannerFailureRecord
from ..sandbox.interfaces import SandboxManager
from ..scheduler.config import SchedulerConfig
from ..scheduler.interfaces import (
    ReadyTask,
    RunningTask,
    ScheduleDecision,
    Scheduler,
    SchedulingSnapshot,
)
from ..scheduler.models import DecisionAction, DeferReason, ModelBudgetState
from ..validation.interfaces import ValidationReport, Validator
from . import events
from .failures import (
    FailureAction,
    FailureClassifier,
    FailureDecision,
    FailureKind,
    FailureRecord,
)
from .models import EngineStatus, OrchestratorConfig, RecoveryAction, RecoveryActionKind
from .recovery import decisions_for_report
from .state_machine import is_legal, is_legal_project
from .support import (
    checks_from_task,
    project_checks,
    repo_handle_for_project,
    spec_from_project,
    task_spec_from_model,
    usage_window_start,
)

_log = get_logger("devsandbox.orchestrator")

Replanner = Callable[[str, list[PlannerFailureRecord]], Awaitable[object]]
SleepFn = Callable[[float], Awaitable[None]]

_TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
    }
)
_ACTIVE_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.SCHEDULED,
        TaskStatus.RUNNING,
        TaskStatus.VALIDATING,
        TaskStatus.VALIDATED,
        TaskStatus.INTEGRATING,
    }
)


class SimulatedCrash(BaseException):
    """Raised by :class:`FaultInjector` to simulate process death in tests.

    Derives from ``BaseException`` so ordinary ``except Exception`` handlers do
    not turn an injected crash into a normal task failure.
    """


class FaultInjector:
    """Deterministic crash-injection hook used by crash-recovery tests."""

    def __init__(self, crash_at: set[str] | None = None, *, once: bool = True) -> None:
        self.crash_at = set(crash_at or ())
        self.once = once
        self.checkpoints: list[str] = []

    def arm(self, name: str) -> None:
        """Arm a checkpoint."""
        self.crash_at.add(name)

    async def checkpoint(self, name: str) -> None:
        """Record a checkpoint and raise when armed."""
        self.checkpoints.append(name)
        if name in self.crash_at:
            if self.once:
                self.crash_at.discard(name)
            raise SimulatedCrash(name)


class OrchestratorEngine:
    """Authoritative execution loop and per-attempt pipeline."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        git: GitManager,
        context: ContextManager,
        agent: AgentAdapter,
        validator: Validator,
        scheduler: Scheduler,
        monitor: Any,
        sandbox: SandboxManager | None = None,
        settings: DevSandboxSettings | None = None,
        policy: SecurityPolicy | None = None,
        config: OrchestratorConfig | None = None,
        scheduler_config: SchedulerConfig | None = None,
        classifier: FailureClassifier | None = None,
        replanner: Replanner | None = None,
        project_service: Any | None = None,
        recovery: Any | None = None,
        faults: FaultInjector | None = None,
        lease_holder: str | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: SleepFn = asyncio.sleep,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.git = git
        self.context = context
        self.agent = agent
        self.validator = validator
        self.scheduler = scheduler
        self.monitor = monitor
        self.sandbox = sandbox
        self.settings = settings or get_settings()
        self.policy = policy or SecurityPolicy(self.settings)
        if self.policy.main_merge_allowed():
            _log.warning(
                "allow_main_merge is enabled, but the orchestrator has no main-branch "
                "merge path in the MVP; the setting is ignored"
            )
        self.config = config or OrchestratorConfig.from_settings(self.settings)
        self.scheduler_config = scheduler_config or SchedulerConfig.from_settings(self.settings)
        self.classifier = classifier or FailureClassifier(
            max_infra_failures=self.config.max_infra_failures,
            repeat_failure_threshold=self.config.repeat_failure_threshold,
            backoff_base_seconds=self.config.backoff_base_seconds,
            backoff_max_seconds=self.config.backoff_max_seconds,
            backoff_jitter_ratio=self.config.backoff_jitter_ratio,
            clock=clock,
            jitter=jitter,
        )
        self.replanner = replanner
        self.project_service = project_service
        self.recovery = recovery
        self.faults = faults or FaultInjector()
        self.lease_holder = lease_holder or (f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}")
        self._clock = clock
        self._sleep = sleep
        self._running: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._shutdown = False
        self._lease_acquired = False
        self._last_lease_refresh: datetime | None = None
        self._deferrals: dict[str, str] = {}
        self._deferral_counts: dict[str, int] = {}
        self._integration_locks: dict[str, asyncio.Lock] = {}
        self._replan_counts: dict[str, int] = {}
        self._launching: set[str] = set()
        # Project id for each in-flight task plus attempts reserved by a launch
        # that has not committed its attempts row yet.  The reservation keeps
        # the per-project attempt circuit breaker exact under concurrency.
        self._running_project: dict[str, str] = {}
        self._reserved_attempts: dict[str, int] = {}
        self._last_round_at: datetime | None = None
        self._last_pressure = "ok"

    # ================================================================== lease
    async def acquire_lease(self) -> None:
        """Acquire the exclusive orchestrator lease, or refuse to start."""
        if self._lease_acquired:
            return
        async with self._session_factory() as session:
            lease = await LeaseRepo(session).try_acquire(
                self.config.lease_name,
                self.lease_holder,
                ttl_seconds=self.config.lease_ttl_seconds,
            )
            if lease is None:
                await session.rollback()
                raise OrchestratorAlreadyRunning(
                    f"another orchestrator holds lease {self.config.lease_name!r}"
                )
            await events.emit_event(
                session,
                project_id="system",
                event_type=events.LEASE_ACQUIRED,
                payload={"holder": self.lease_holder},
            )
            await session.commit()
        self._lease_acquired = True
        self._last_lease_refresh = self._clock()

    async def release_lease(self) -> None:
        """Release the lease if this engine still owns it."""
        if not self._lease_acquired:
            return
        async with self._session_factory() as session:
            await LeaseRepo(session).release(self.config.lease_name, self.lease_holder)
            await session.commit()
        self._lease_acquired = False

    async def _refresh_lease(self) -> None:
        if not self._lease_acquired:
            return
        now = self._clock()
        if (
            self._last_lease_refresh is not None
            and (now - self._last_lease_refresh).total_seconds() < self.config.lease_ttl_seconds / 2
        ):
            return
        async with self._session_factory() as session:
            refreshed = await LeaseRepo(session).refresh(
                self.config.lease_name,
                self.lease_holder,
                ttl_seconds=self.config.lease_ttl_seconds,
            )
            await session.commit()
        if not refreshed:
            raise OrchestratorAlreadyRunning("orchestrator lease was lost")
        self._last_lease_refresh = now

    # ================================================================== lifecycle
    async def run_until_idle(self, *, max_rounds: int = 1000) -> None:
        """Run recovery + rounds until no task can make progress (tests/CLI)."""
        await self.acquire_lease()
        await self._recover()
        await self._emit_engine_event(events.ENGINE_STARTED)
        rounds = 0
        while rounds < max_rounds:
            await self._await_running()
            if not await self._has_work():
                break
            launched = await self._round()
            rounds += 1
            await self._await_running()
            if launched == 0 and not self._running:
                delay = await self._next_retry_delay()
                if delay is None:
                    break
                # Wait for the earliest backoff deadline; a fake sleep may
                # advance the injected clock so this converges in tests.
                await self._sleep(min(max(delay, 0.0), self.config.tick_seconds) or 0.001)
                continue
            await self._sleep(self.config.idle_sleep_seconds)
        await self._await_running()
        await self._finalize_projects()
        await self._emit_engine_event(events.ENGINE_STOPPED)
        await self.release_lease()

    async def _next_retry_delay(self) -> float | None:
        """Return seconds until the next RETRY_WAIT deadline, if any."""
        async with self._session_factory() as session:
            tasks = await TaskRepo(session).list_by_status([TaskStatus.RETRY_WAIT])
            if not tasks:
                return None
            now = self._clock()
            deadlines = [
                max(0.0, (task.retry_not_before - now).total_seconds())
                for task in tasks
                if task.retry_not_before is not None
            ]
            return min(deadlines) if deadlines else 0.0

    async def run_forever(self) -> None:
        """Run until :meth:`request_shutdown` is called."""
        await self.acquire_lease()
        await self._recover()
        await self._emit_engine_event(events.ENGINE_STARTED)
        try:
            while not self._shutdown:
                await self._refresh_lease()
                launched = await self._round()
                await self._finalize_projects()
                if launched:
                    await self._sleep(0)
                    continue
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.config.tick_seconds)
        finally:
            await self.shutdown()

    async def request_shutdown(self) -> None:
        """Stop launching new attempts."""
        self._shutdown = True
        self._wake.set()

    async def shutdown(self) -> None:
        """Gracefully stop: wait briefly, then cancel and persist state."""
        self._shutdown = True
        self._wake.set()
        pending = list(self._running.values())
        if pending:
            _, still_running = await asyncio.wait(
                pending, timeout=self.config.shutdown_timeout_seconds
            )
            for task in still_running:
                task.cancel()
            if still_running:
                await asyncio.gather(*still_running, return_exceptions=True)
        await self._emit_engine_event(events.ENGINE_STOPPED)
        await self.release_lease()

    # ================================================================== rounds
    async def run_once(self) -> int:
        """Run one scheduling round and return the number of launched attempts."""
        if not self._lease_acquired:
            await self.acquire_lease()
        return await self._round()

    async def _round(self) -> int:
        await self._refresh_lease()
        await self._refresh_readiness()
        snapshot = await self._build_snapshot()
        decisions = await self.scheduler.select(snapshot)
        launched = 0
        for decision in decisions:
            if decision.action is DecisionAction.LAUNCH:
                self._deferrals.pop(decision.task_id, None)
                self._deferral_counts.pop(decision.task_id, None)
                if await self._launch(decision.task_id):
                    launched += 1
            else:
                await self._record_defer(decision)
        self._last_round_at = self._clock()
        self._last_pressure = snapshot.pressure.value
        self._wake.clear()
        return launched

    async def _refresh_readiness(self) -> None:
        """PENDING -> READY, due RETRY_WAIT -> READY, failed deps -> BLOCKED."""
        now = self._clock()
        async with self._session_factory() as session:
            projects = await ProjectRepo(session, is_legal_project).list_by_status(
                [ProjectStatus.EXECUTING]
            )
            tasks_repo = TaskRepo(session, is_legal)
            changed = False
            for project in projects:
                if project.active_plan_id is None:
                    continue
                tasks = await tasks_repo.list_for_plan(project.active_plan_id)
                by_id = {task.id: task for task in tasks}
                for task in tasks:
                    status = TaskStatus(task.status)
                    deps = await tasks_repo.dependencies_for_task(task.id)
                    dep_statuses = [TaskStatus(by_id[dep].status) for dep in deps if dep in by_id]
                    if status in {TaskStatus.PENDING, TaskStatus.READY} and any(
                        dep in {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
                        for dep in dep_statuses
                    ):
                        with contextlib.suppress(InvalidTransition):
                            await tasks_repo.transition(
                                TaskId(task.id),
                                [TaskStatus.PENDING, TaskStatus.READY],
                                TaskStatus.BLOCKED,
                                "dependency failed or cancelled",
                                blocked_reason="dependency failed or cancelled",
                            )
                            changed = True
                        continue
                    if status is TaskStatus.PENDING and all(
                        dep is TaskStatus.COMPLETED for dep in dep_statuses
                    ):
                        with contextlib.suppress(InvalidTransition):
                            await tasks_repo.transition(
                                TaskId(task.id),
                                [TaskStatus.PENDING],
                                TaskStatus.READY,
                                "dependencies completed",
                            )
                            changed = True
                    elif status is TaskStatus.RETRY_WAIT and (
                        task.retry_not_before is None or task.retry_not_before <= now
                    ):
                        with contextlib.suppress(InvalidTransition):
                            await tasks_repo.transition(
                                TaskId(task.id),
                                [TaskStatus.RETRY_WAIT],
                                TaskStatus.READY,
                                "backoff elapsed",
                                retry_not_before=None,
                            )
                            changed = True
            if changed:
                await session.commit()

    async def _build_snapshot(self) -> SchedulingSnapshot:
        now = self._clock()
        host = await self.monitor.sample()
        pressure = await self.monitor.pressure_state()
        async with self._session_factory() as session:
            projects = await ProjectRepo(session, is_legal_project).list_by_status(
                [ProjectStatus.EXECUTING, ProjectStatus.PAUSED]
            )
            paused = [
                project.id
                for project in projects
                if ProjectStatus(project.status) is ProjectStatus.PAUSED
            ]
            executing = {
                project.id: project
                for project in projects
                if ProjectStatus(project.status) is ProjectStatus.EXECUTING
            }
            ready: list[ReadyTask] = []
            running: list[RunningTask] = []
            for project_id, project in executing.items():
                if project.active_plan_id is None:
                    continue
                tasks_repo = TaskRepo(session, is_legal)
                tasks = await tasks_repo.list_for_plan(project.active_plan_id)
                dependencies = {
                    task.id: await tasks_repo.dependencies_for_task(task.id) for task in tasks
                }
                depths = _critical_path_lengths(tasks, dependencies)
                for index, task in enumerate(tasks):
                    status = TaskStatus(task.status)
                    if status is TaskStatus.READY:
                        ready.append(
                            ReadyTask(
                                task_id=task.id,
                                project_id=project_id,
                                priority=task.priority,
                                created_order=index,
                                resources=_task_resources(task),
                                retry_not_before=task.retry_not_before,
                                attempt_count=task.attempt_count,
                                critical_path_length=depths.get(task.id, 0),
                                consecutive_deferrals=self._deferral_counts.get(task.id, 0),
                            )
                        )
                    elif status in _ACTIVE_TASK_STATUSES:
                        running.append(
                            RunningTask(
                                task_id=task.id,
                                project_id=project_id,
                                resources=_task_resources(task),
                                priority=task.priority,
                                created_order=index,
                            )
                        )
            budget = await self._model_budget(session, list(executing))
        return SchedulingSnapshot(
            now=now,
            host=host,
            pressure=pressure,
            ready_tasks=ready,
            running=running,
            limits=self.scheduler_config.scheduling_limits(),
            model_budget=budget,
            paused_projects=paused,
            project_modes={project.id: ProjectMode(project.mode) for project in executing.values()},
        )

    async def _record_defer(self, decision: ScheduleDecision) -> None:
        reason = (
            decision.reason_code.value
            if decision.reason_code is not None
            else decision.reason or "deferred"
        )
        if self._deferrals.get(decision.task_id) == reason:
            self._deferral_counts[decision.task_id] = (
                self._deferral_counts.get(decision.task_id, 0) + 1
            )
            return
        self._deferrals[decision.task_id] = reason
        self._deferral_counts[decision.task_id] = 1
        async with self._session_factory() as session:
            task = await TaskRepo(session).get(TaskId(decision.task_id))
            project_id = task.project_id if task is not None else "unknown"
            await events.emit_event(
                session,
                project_id=project_id,
                task_id=decision.task_id,
                event_type=events.SCHEDULE_DECISION,
                payload={
                    "action": decision.action.value,
                    "reason_code": reason,
                    "reason": decision.reason,
                },
            )
            await session.commit()

    # ================================================================== launch
    async def _launch(self, task_id: str) -> bool:
        if task_id in self._running or task_id in self._launching:
            return False
        self._launching.add(task_id)
        try:
            return await self._launch_locked(task_id)
        finally:
            self._launching.discard(task_id)

    async def _launch_locked(self, task_id: str) -> bool:
        task = await self._load_task(TaskId(task_id))
        if task is None or TaskStatus(task.status) is not TaskStatus.READY:
            return False
        project = await self._load_project(task.project_id)
        if project is None or ProjectStatus(project.status) is not ProjectStatus.EXECUTING:
            return False
        async with self._session_factory() as session:
            attempts = await AttemptRepo(session).count_for_project(task.project_id)
            reserved = self._reserved_attempts.get(task.project_id, 0)
            over_budget = await self._budget_exceeded(task.project_id, session=session)
            if over_budget:
                await events.emit_event(
                    session,
                    project_id=task.project_id,
                    task_id=task.id,
                    event_type=events.SCHEDULE_DECISION,
                    payload={
                        "action": "defer",
                        "reason_code": DeferReason.MODEL_BUDGET.value,
                        "reason": "project model budget exhausted",
                    },
                )
                await session.commit()
                return False
            if attempts >= self.config.max_total_attempts:
                await events.emit_event(
                    session,
                    project_id=task.project_id,
                    event_type=events.CIRCUIT_BREAKER,
                    payload={
                        "reason": "max_total_attempts reached",
                        "attempts": attempts,
                        "limit": self.config.max_total_attempts,
                    },
                )
                with contextlib.suppress(InvalidTransition):
                    await ProjectRepo(session, is_legal_project).transition(
                        task.project_id,
                        [ProjectStatus.EXECUTING],
                        ProjectStatus.PAUSED,
                        "global attempt circuit breaker",
                    )
                await session.commit()
                return False
            if attempts + reserved >= self.config.max_total_attempts:
                # At capacity because launched attempts have not persisted their
                # rows yet; leave the task queued for the next round.
                return False
        self._reserve_attempt(task.id, task.project_id)
        runner = asyncio.create_task(
            self._attempt_runner(task.id),
            name=f"ds-attempt-{task.id}",
        )
        self._running[task.id] = runner
        runner.add_done_callback(lambda _: self._wake.set())
        return True

    def _reserve_attempt(self, task_id: str, project_id: str) -> None:
        self._running_project[task_id] = project_id
        self._reserved_attempts[project_id] = self._reserved_attempts.get(project_id, 0) + 1

    def _release_attempt_reservation(self, task_id: str) -> None:
        """Release a launch reservation once the attempts row is committed."""
        project_id = self._running_project.pop(task_id, None)
        if project_id is None:
            return
        remaining = self._reserved_attempts.get(project_id, 0) - 1
        if remaining > 0:
            self._reserved_attempts[project_id] = remaining
        else:
            self._reserved_attempts.pop(project_id, None)

    # ================================================================== pipeline
    async def _attempt_runner(self, task_id: str, resume: RecoveryAction | None = None) -> None:
        try:
            if resume is None:
                await self._run_attempt(TaskId(task_id))
            else:
                await self._resume_action(TaskId(task_id), resume)
        except SimulatedCrash:
            raise
        except asyncio.CancelledError:
            await asyncio.shield(self._persist_shutdown_cancel(task_id))
            raise
        except Exception as exc:  # never let an attempt crash the loop
            await asyncio.shield(self._unexpected_failure(task_id, exc))
        finally:
            self._release_attempt_reservation(task_id)
            self._running.pop(task_id, None)
            self._wake.set()

    async def _run_attempt(self, task_id: TaskId) -> None:
        task = await self._load_task(task_id)
        if task is None or TaskStatus(task.status) is not TaskStatus.READY:
            return
        project = await self._load_project(task.project_id)
        if project is None or ProjectStatus(project.status) is not ProjectStatus.EXECUTING:
            return
        repo = repo_handle_for_project(project)
        spec = spec_from_project(project)
        number = await self._next_attempt_number(task_id)
        attempt_id = AttemptId(f"{task_id}-a{number}")
        workspace: Workspace | None = None

        # 1. Persist intent (attempt row + READY -> SCHEDULED) before any side effect.
        async with self._session_factory() as session:
            now = self._clock()
            await AttemptRepo(session).create(
                AttemptModel(
                    id=attempt_id,
                    task_id=str(task_id),
                    number=number,
                    status="scheduled",
                    agent_name=self.agent.name,
                    started_at=now,
                )
            )
            try:
                updated = await TaskRepo(session, is_legal).transition(
                    task_id,
                    [TaskStatus.READY],
                    TaskStatus.SCHEDULED,
                    f"attempt {number} scheduled",
                    current_attempt_id=attempt_id,
                    retry_not_before=None,
                )
            except InvalidTransition:
                await session.rollback()
                return
            await events.emit_event(
                session,
                project_id=task.project_id,
                task_id=str(task_id),
                attempt_id=attempt_id,
                event_type=events.ATTEMPT_CREATED,
                payload={"number": number, "agent": self.agent.name},
            )
            await session.commit()
            base_attempt = updated.attempt_count
        # The attempts row is now durable; the DB count includes it.
        self._release_attempt_reservation(str(task_id))
        await self.faults.checkpoint("after_attempt_created")
        await self.faults.checkpoint("after_attempt_row")

        # 2. Workspace + context bundle (recoverable: SCHEDULED -> READY on infra).
        try:
            base_ref = await self.git.staging_head(repo) or repo.base_commit or repo.ref
            workspace = await self.git.create_workspace(repo, attempt_id, base_ref)
            async with self._session_factory() as session:
                attempt = await AttemptRepo(session).get(str(attempt_id))
                if attempt is not None:
                    attempt.workspace_path = workspace.path
                    attempt.branch = workspace.branch
                    attempt.base_commit = workspace.base_commit
                await session.commit()
            bundle = await self.context.get_task_context(
                task.project_id, task_spec_from_model(task), workspace.base_commit
            )
        except Exception as exc:
            await self._handle_failure(
                task_id,
                attempt_id,
                self.classifier.from_exception(exc),
                stage="workspace",
                pre_agent=True,
            )
            return
        await self.faults.checkpoint("after_workspace")

        request, denied = self._build_agent_request(
            task, workspace, bundle, attempt_number=number, project=project
        )
        if denied:
            await self._emit_policy_denied(task, attempt_id, denied)

        # 3. SCHEDULED -> RUNNING, then run the agent.
        async with self._session_factory() as session:
            try:
                await TaskRepo(session, is_legal).transition(
                    task_id,
                    [TaskStatus.SCHEDULED],
                    TaskStatus.RUNNING,
                    "agent started",
                    attempt_count=base_attempt + 1,
                )
            except InvalidTransition:
                await session.rollback()
                return
            attempt = await AttemptRepo(session).get(str(attempt_id))
            if attempt is not None:
                attempt.status = "running"
                attempt.started_at = self._clock()
                attempt.container_id = f"ds-{attempt_id}"
            await events.emit_event(
                session,
                project_id=task.project_id,
                task_id=str(task_id),
                attempt_id=attempt_id,
                event_type=events.AGENT_STARTED,
                payload={
                    "agent": self.agent.name,
                    "container": f"ds-{attempt_id}",
                    "attempt_number": number,
                },
            )
            await session.commit()
        await self.faults.checkpoint("before_agent")

        try:
            result = await self.agent.run(request)
        except Exception as exc:
            await self._handle_failure(
                task_id,
                attempt_id,
                self.classifier.from_exception(exc),
                stage="agent",
                pre_agent=False,
            )
            return
        await self.faults.checkpoint("after_agent")
        await self._persist_agent_result(task, attempt_id, result)
        record = self.classifier.from_agent_result(result)
        if record is not None:
            await self._handle_failure(task_id, attempt_id, record, stage="agent", pre_agent=False)
            return
        await self._continue_collect(task_id, attempt_id, workspace, spec, repo)

    async def _resume_action(self, task_id: TaskId, action: RecoveryAction) -> None:
        """Continue an attempt from the recovery decision point."""
        if action.kind in {
            RecoveryActionKind.REVERT_READY,
            RecoveryActionKind.KILL_AND_RETRY,
            RecoveryActionKind.RETRY_INFRA,
            RecoveryActionKind.MARK_COMPLETED,
            RecoveryActionKind.NONE,
        }:
            return
        task = await self._load_task(task_id)
        if task is None:
            return
        attempt = await self._load_attempt(action.attempt_id or task.current_attempt_id)
        if attempt is None:
            return
        project = await self._load_project(task.project_id)
        if project is None:
            return
        repo = repo_handle_for_project(project)
        spec = spec_from_project(project)
        attempt_id = AttemptId(attempt.id)
        if not attempt.workspace_path:
            await self._handle_failure(
                task_id,
                attempt_id,
                self.classifier.make_record(
                    FailureKind.INFRA_ERROR,
                    "recovery found no workspace path",
                    retryable=True,
                    counts_against_budget=False,
                ),
                stage="recovery",
                pre_agent=False,
            )
            return
        workspace = Workspace(
            attempt_id=attempt_id,
            path=attempt.workspace_path,
            base_commit=attempt.base_commit or "",
            branch=attempt.branch or "",
        )
        try:
            changes = await self.git.collect_changes(workspace)
        except Exception as exc:
            await self._handle_failure(
                task_id,
                attempt_id,
                self.classifier.from_exception(exc),
                stage="recovery",
                pre_agent=False,
            )
            return

        if action.kind is RecoveryActionKind.RESUME_COLLECT:
            await self._continue_collect(task_id, attempt_id, workspace, spec, repo)
        elif action.kind is RecoveryActionKind.RERUN_VALIDATION:
            await self._resume_validation(
                task, attempt, workspace, changes, spec, repo, report=None
            )
        elif action.kind is RecoveryActionKind.RESUME_VALIDATION_DECISION:
            report = self._persisted_report(attempt)
            await self._resume_validation(
                task, attempt, workspace, changes, spec, repo, report=report
            )
        elif action.kind in {
            RecoveryActionKind.RESUME_INTEGRATION,
            RecoveryActionKind.RESET_AND_RETRY,
        }:
            status = TaskStatus(task.status)
            if status is TaskStatus.VALIDATED:
                already_integrating = False
            elif status is TaskStatus.INTEGRATING:
                already_integrating = True
            else:
                await self._resume_validation(
                    task,
                    attempt,
                    workspace,
                    changes,
                    spec,
                    repo,
                    report=self._persisted_report(attempt),
                )
                return
            await self._continue_integration(
                task_id,
                attempt_id,
                workspace,
                changes,
                spec,
                repo,
                already_integrating=already_integrating,
            )

    async def _resume_validation(
        self,
        task: TaskModel,
        attempt: AttemptModel,
        workspace: Workspace,
        changes: ChangeSet,
        spec: Any,
        repo: RepoHandle,
        *,
        report: ValidationReport | None,
    ) -> None:
        """Continue validation from a persisted report or by re-running it."""
        task_id = TaskId(task.id)
        attempt_id = AttemptId(attempt.id)
        if report is None:
            try:
                report = await self.validator.validate_task(
                    task_spec_from_model(task), workspace, changes
                )
            except Exception as exc:
                await self._handle_failure(
                    task_id,
                    attempt_id,
                    self.classifier.from_exception(exc),
                    stage="validation",
                    pre_agent=False,
                )
                return
            await self._persist_validation(attempt_id, report)
            task = await self._load_task(task_id) or task
        if report.status != "passed":
            await self._handle_failure(
                task_id,
                attempt_id,
                self.classifier.from_validation(report),
                stage="validation",
                pre_agent=False,
            )
            return
        status = TaskStatus(task.status)
        if status is TaskStatus.VALIDATING:
            async with self._session_factory() as session:
                try:
                    await TaskRepo(session, is_legal).transition(
                        task_id,
                        [TaskStatus.VALIDATING],
                        TaskStatus.VALIDATED,
                        "recovery: persisted validation passed",
                    )
                except InvalidTransition:
                    await session.rollback()
                    return
                attempt_model = await AttemptRepo(session).get(str(attempt_id))
                if attempt_model is not None:
                    attempt_model.status = "validated"
                await session.commit()
            status = TaskStatus.VALIDATED
        await self._continue_integration(
            task_id,
            attempt_id,
            workspace,
            changes,
            spec,
            repo,
            already_integrating=status is TaskStatus.INTEGRATING,
        )

    @staticmethod
    def _persisted_report(attempt: AttemptModel) -> ValidationReport | None:
        if not attempt.validation_report_json:
            return None
        try:
            return ValidationReport.model_validate(attempt.validation_report_json)
        except Exception:
            return None

    async def _continue_collect(
        self,
        task_id: TaskId,
        attempt_id: AttemptId,
        workspace: Workspace,
        spec: Any,
        repo: RepoHandle,
    ) -> None:
        task = await self._load_task(task_id)
        if task is None or TaskStatus(task.status) is not TaskStatus.RUNNING:
            return
        try:
            changes = await self.git.collect_changes(workspace)
            restricted = list(task.restricted_files_json or []) + list(
                spec.constraints.restricted_paths
            )
            violations = await self.git.check_restricted_paths(changes, restricted)
        except Exception as exc:
            await self._handle_failure(
                task_id,
                attempt_id,
                self.classifier.from_exception(exc),
                stage="collect",
                pre_agent=False,
            )
            return
        if violations:
            record = self.classifier.policy_denied(
                "; ".join(violations),
                detail={"violations": violations, "files_changed": changes.files_changed},
            )
            await self._handle_failure(
                task_id, attempt_id, record, stage="restricted_paths", pre_agent=False
            )
            return
        await self.faults.checkpoint("after_collect")
        async with self._session_factory() as session:
            attempt = await AttemptRepo(session).get(str(attempt_id))
            if attempt is not None:
                attempt.head_commit = changes.head_commit
            try:
                await TaskRepo(session, is_legal).transition(
                    task_id,
                    [TaskStatus.RUNNING],
                    TaskStatus.VALIDATING,
                    "changes collected",
                )
            except InvalidTransition:
                await session.rollback()
                return
            if attempt is not None:
                attempt.status = "validating"
            await events.emit_event(
                session,
                project_id=task.project_id,
                task_id=str(task_id),
                attempt_id=attempt_id,
                event_type=events.VALIDATION_STARTED,
                payload={"files_changed": len(changes.files_changed)},
            )
            await session.commit()
        await self.faults.checkpoint("before_validation")

        try:
            report = await self.validator.validate_task(
                task_spec_from_model(task), workspace, changes
            )
        except Exception as exc:
            await self._handle_failure(
                task_id,
                attempt_id,
                self.classifier.from_exception(exc),
                stage="validation",
                pre_agent=False,
            )
            return
        await self._persist_validation(attempt_id, report)
        await self.faults.checkpoint("after_validation")
        await self.faults.checkpoint("after_validation_report")
        if report.status != "passed":
            await self._handle_failure(
                task_id,
                attempt_id,
                self.classifier.from_validation(report),
                stage="validation",
                pre_agent=False,
            )
            return
        async with self._session_factory() as session:
            try:
                await TaskRepo(session, is_legal).transition(
                    task_id,
                    [TaskStatus.VALIDATING],
                    TaskStatus.VALIDATED,
                    "validation passed",
                )
            except InvalidTransition:
                await session.rollback()
                return
            attempt = await AttemptRepo(session).get(str(attempt_id))
            if attempt is not None:
                attempt.status = "validated"
            await session.commit()
        await self._continue_integration(
            task_id,
            attempt_id,
            workspace,
            changes,
            spec,
            repo,
            already_integrating=False,
        )

    async def _continue_integration(
        self,
        task_id: TaskId,
        attempt_id: AttemptId,
        workspace: Workspace,
        changes: ChangeSet,
        spec: Any,
        repo: RepoHandle,
        *,
        already_integrating: bool,
    ) -> None:
        """Serialize integration per project, then run the integration step."""
        task = await self._load_task(task_id)
        if task is None:
            return
        lock = self._integration_locks.setdefault(task.project_id, asyncio.Lock())
        async with lock:
            await self._continue_integration_locked(
                task_id,
                attempt_id,
                workspace,
                changes,
                spec,
                repo,
                already_integrating=already_integrating,
            )

    async def _continue_integration_locked(
        self,
        task_id: TaskId,
        attempt_id: AttemptId,
        workspace: Workspace,
        changes: ChangeSet,
        spec: Any,
        repo: RepoHandle,
        *,
        already_integrating: bool,
    ) -> None:
        task = await self._load_task(task_id)
        if task is None:
            return
        status = TaskStatus(task.status)
        if not already_integrating:
            if status is not TaskStatus.VALIDATED:
                return
            async with self._session_factory() as session:
                try:
                    await TaskRepo(session, is_legal).transition(
                        task_id,
                        [TaskStatus.VALIDATED],
                        TaskStatus.INTEGRATING,
                        "integration started",
                    )
                except InvalidTransition:
                    await session.rollback()
                    return
                attempt = await AttemptRepo(session).get(str(attempt_id))
                if attempt is not None:
                    attempt.status = "integrating"
                await session.commit()
        elif status is not TaskStatus.INTEGRATING:
            return

        staging_before = await self.git.staging_head(repo)
        integration_id = await self._create_integration(
            task, attempt_id, staging_before, status="preparing"
        )
        await self.faults.checkpoint("before_integration")

        prepare = getattr(self.git, "prepare_integration", None)
        candidate: Any | None = None
        try:
            if callable(prepare):
                candidate = await prepare(repo, workspace, changes)
                await self.faults.checkpoint("after_prepare_integration")
                if candidate.conflicted:
                    abort = getattr(self.git, "abort_integration", None)
                    if callable(abort):
                        await abort(candidate)
                    await self._update_integration(
                        integration_id,
                        status="conflict",
                        staging_after=candidate.staging_after,
                        conflicts=list(candidate.conflicting_files),
                        detail="staging merge conflict",
                    )
                    await self._handle_failure(
                        task_id,
                        attempt_id,
                        self.classifier.from_integration(
                            IntegrationResult(
                                status="conflict",
                                conflicting_files=list(candidate.conflicting_files),
                                detail="staging merge conflict",
                            )
                        ),
                        stage="integration",
                        pre_agent=False,
                    )
                    return
                await self._update_integration(
                    integration_id,
                    status="prepared",
                    staging_after=candidate.staging_after,
                    conflicts=[],
                    detail="candidate prepared",
                )
                checks = self._integration_checks(task, spec)
                report = await self.validator.validate_integration(
                    repo, candidate.staging_after, checks
                )
                await self._persist_validation(attempt_id, report)
                if report.status != "passed":
                    abort = getattr(self.git, "abort_integration", None)
                    if callable(abort):
                        await abort(candidate)
                    await self._update_integration(
                        integration_id,
                        status="validation_failed",
                        detail=report.summary,
                    )
                    await self._handle_failure(
                        task_id,
                        attempt_id,
                        self.classifier.from_validation(report),
                        stage="integration_validation",
                        pre_agent=False,
                    )
                    return
                commit = getattr(self.git, "commit_integration", None)
                if not callable(commit):
                    raise RuntimeError("git manager exposes no commit_integration")
                result = await commit(candidate)
            else:
                result = await self.git.integrate_to_staging(repo, workspace, changes)
            await self.faults.checkpoint("after_staging_advance_before_db")
        except Exception as exc:
            await self._handle_failure(
                task_id,
                attempt_id,
                self.classifier.from_exception(exc),
                stage="integration",
                pre_agent=False,
            )
            return
        await self._complete_integration(
            task, attempt_id, workspace, changes, integration_id, result
        )

    async def _complete_integration(
        self,
        task: TaskModel,
        attempt_id: AttemptId,
        workspace: Workspace,
        changes: ChangeSet,
        integration_id: str,
        result: IntegrationResult,
    ) -> None:
        if result.status != "integrated":
            await self._update_integration(
                integration_id,
                status=result.status,
                conflicts=list(result.conflicting_files),
                detail=result.detail,
            )
            await self._handle_failure(
                TaskId(task.id),
                attempt_id,
                self.classifier.from_integration(result),
                stage="integration",
                pre_agent=False,
            )
            return
        async with self._session_factory() as session:
            await self._update_integration(
                integration_id,
                status="integrated",
                staging_after=result.staging_commit,
                detail=result.detail,
                session=session,
            )
            try:
                await TaskRepo(session, is_legal).transition(
                    TaskId(task.id),
                    [TaskStatus.INTEGRATING],
                    TaskStatus.COMPLETED,
                    "integration committed",
                    result_json={
                        "staging_commit": result.staging_commit,
                        "files_changed": changes.files_changed,
                    },
                )
            except InvalidTransition:
                await session.rollback()
                return
            attempt = await AttemptRepo(session).get(str(attempt_id))
            if attempt is not None:
                attempt.status = "completed"
                attempt.finished_at = self._clock()
                attempt.head_commit = changes.head_commit
            await events.emit_event(
                session,
                project_id=task.project_id,
                task_id=task.id,
                attempt_id=str(attempt_id),
                event_type=events.INTEGRATION_FINISHED,
                payload={
                    "status": "integrated",
                    "staging_commit": result.staging_commit,
                    "files_changed": changes.files_changed,
                },
            )
            await session.commit()
        await self.faults.checkpoint("after_integration_commit")

        outcome = TaskOutcomeNote(
            task_id=task.id,
            outcome=TaskOutcome.SUCCESS,
            revision=result.staging_commit or changes.head_commit or "",
            files_changed=list(changes.files_changed),
            commit_ids=[commit.sha for commit in changes.commits],
            validation_summary="validation passed and integration committed",
            agent_summary="",
            recorded_at=self._clock(),
        )
        with contextlib.suppress(Exception):
            await self.context.record_task_outcome(task.project_id, task.id, outcome)
        async with self._session_factory() as session:
            await events.emit_event(
                session,
                project_id=task.project_id,
                task_id=task.id,
                attempt_id=str(attempt_id),
                event_type=events.TASK_OUTCOME_RECORDED,
                payload=outcome.model_dump(mode="json"),
            )
            await session.commit()
        await self._remove_workspace(workspace, keep=False)
        await self._destroy_container(str(attempt_id))

    # ================================================================== failures
    async def _handle_failure(
        self,
        task_id: TaskId,
        attempt_id: AttemptId,
        record: FailureRecord,
        *,
        stage: str,
        pre_agent: bool,
    ) -> FailureDecision:
        task = await self._load_task(task_id)
        if task is None:
            return FailureDecision(action=FailureAction.FAIL, record=record)
        previous = dict(task.failure_json or {})
        infra_failures = int(previous.get("infra_failures", 0))
        max_retries = self.policy.max_retries(task.max_retries)
        decision = self.classifier.decide(
            record,
            attempt_count=task.attempt_count,
            max_retries=max_retries,
            previous_failure=previous,
            infra_failures=infra_failures,
        )
        consecutive = 1
        if record.signature and previous.get("signature") == record.signature:
            consecutive = int(previous.get("consecutive_identical", 1)) + 1
        failure_payload = {
            "kind": decision.record.kind.value,
            "summary": decision.record.summary,
            "signature": record.signature,
            "consecutive_identical": consecutive,
            "infra_failures": infra_failures + (0 if record.counts_against_budget else 1),
            "stage": stage,
            "diagnostics_path": decision.record.diagnostics_path,
            "detail": decision.record.detail,
        }
        current = TaskStatus(task.status)
        if decision.action is FailureAction.RETRY:
            target = TaskStatus.READY if pre_agent else TaskStatus.RETRY_WAIT
            from_states = [TaskStatus.SCHEDULED] if pre_agent else [current]
            fields: dict[str, Any] = {
                "retry_not_before": decision.retry_not_before,
                "failure_json": failure_payload,
            }
            if target is TaskStatus.READY:
                fields["current_attempt_id"] = None
            async with self._session_factory() as session:
                try:
                    await TaskRepo(session, is_legal).transition(
                        task_id,
                        from_states,
                        target,
                        f"{stage} failure: {decision.record.summary}",
                        **fields,
                    )
                except InvalidTransition:
                    await session.rollback()
                else:
                    await self._mark_attempt_failed(
                        session, attempt_id, decision.record, abandoned=pre_agent
                    )
                    await events.emit_event(
                        session,
                        project_id=task.project_id,
                        task_id=str(task_id),
                        attempt_id=attempt_id,
                        event_type=events.TASK_RETRY_SCHEDULED,
                        payload={
                            **failure_payload,
                            "retry_not_before": (
                                decision.retry_not_before.isoformat()
                                if decision.retry_not_before
                                else None
                            ),
                            "reason": decision.reason,
                        },
                    )
                    await session.commit()
        elif decision.action is FailureAction.DEFER:
            # Budget pressure: park in RETRY_WAIT without consuming the budget.
            async with self._session_factory() as session:
                with contextlib.suppress(InvalidTransition):
                    await TaskRepo(session, is_legal).transition(
                        task_id,
                        [current],
                        TaskStatus.RETRY_WAIT,
                        f"deferred: {decision.reason}",
                        retry_not_before=decision.retry_not_before,
                        failure_json=failure_payload,
                    )
                    await self._mark_attempt_failed(
                        session, attempt_id, decision.record, abandoned=False
                    )
                    await events.emit_event(
                        session,
                        project_id=task.project_id,
                        task_id=str(task_id),
                        attempt_id=attempt_id,
                        event_type=events.TASK_RETRY_SCHEDULED,
                        payload={**failure_payload, "reason": decision.reason},
                    )
                    await session.commit()
        else:
            await self._exhaust_task(task, attempt_id, decision, failure_payload, stage=stage)
        if decision.action is not FailureAction.RETRY:
            await self._destroy_container(str(attempt_id))
        return decision

    async def _exhaust_task(
        self,
        task: TaskModel,
        attempt_id: AttemptId,
        decision: FailureDecision,
        failure_payload: dict[str, Any],
        *,
        stage: str,
    ) -> None:
        current = TaskStatus(task.status)
        async with self._session_factory() as session:
            try:
                await TaskRepo(session, is_legal).transition(
                    TaskId(task.id),
                    [current],
                    TaskStatus.FAILED,
                    f"{stage} failure exhausted: {decision.record.summary}",
                    failure_json=failure_payload,
                    retry_not_before=None,
                )
            except InvalidTransition:
                await session.rollback()
                return
            await self._mark_attempt_failed(session, attempt_id, decision.record, abandoned=False)
            await events.emit_event(
                session,
                project_id=task.project_id,
                task_id=task.id,
                attempt_id=str(attempt_id),
                event_type=events.TASK_EXHAUSTED,
                payload={**failure_payload, "reason": decision.reason},
            )
            blocked = await TaskRepo(session, is_legal).mark_dependents_blocked(
                task.id, reason=f"dependency {task.key} failed"
            )
            if blocked:
                await events.emit_event(
                    session,
                    project_id=task.project_id,
                    task_id=task.id,
                    event_type=events.DEPENDENTS_BLOCKED,
                    payload={"blocked": [row.id for row in blocked]},
                )
            await session.commit()
        if self.project_service is not None:
            project = await self._load_project(task.project_id)
            supervised = project is None or ProjectMode(project.mode) is not ProjectMode.AUTONOMOUS
            if supervised or decision.record.requires_attention:
                with contextlib.suppress(Exception):
                    await self.project_service.needs_attention(
                        task.project_id, decision.record.summary
                    )
        await self._maybe_replan(task, decision.record)

    async def _maybe_replan(self, task: TaskModel, record: FailureRecord) -> None:
        if self.replanner is None:
            return
        project = await self._load_project(task.project_id)
        if project is None or ProjectMode(project.mode) is not ProjectMode.AUTONOMOUS:
            return
        used = self._replan_counts.get(task.project_id, 0)
        if used >= self.config.max_replans:
            async with self._session_factory() as session:
                await events.emit_event(
                    session,
                    project_id=task.project_id,
                    event_type=events.CIRCUIT_BREAKER,
                    payload={"reason": "max_replans reached", "replans": used},
                )
                await session.commit()
            return
        self._replan_counts[task.project_id] = used + 1
        failure = PlannerFailureRecord(
            task_id=task.id,
            task_key=task.key,
            summary=record.summary,
            error=record.kind.value,
            validation_summary=record.summary,
        )
        try:
            result = await self.replanner(task.project_id, [failure])
        except Exception as exc:
            async with self._session_factory() as session:
                await events.emit_event(
                    session,
                    project_id=task.project_id,
                    event_type=events.PROJECT_REPLANNED,
                    payload={"error": str(exc)},
                )
                await session.commit()
            return
        async with self._session_factory() as session:
            await events.emit_event(
                session,
                project_id=task.project_id,
                event_type=events.PROJECT_REPLANNED,
                payload={
                    "plan_id": getattr(result, "id", None),
                    "version": getattr(result, "version", None),
                    "failures": [failure.model_dump(mode="json")],
                },
            )
            await session.commit()

    async def _unexpected_failure(self, task_id: str, exc: BaseException) -> None:
        task = await self._load_task(TaskId(task_id))
        if task is None:
            return
        attempt_id = AttemptId(task.current_attempt_id or f"{task_id}-unknown")
        await self._handle_failure(
            TaskId(task_id),
            attempt_id,
            self.classifier.from_exception(exc),
            stage="unexpected",
            pre_agent=TaskStatus(task.status) is TaskStatus.SCHEDULED,
        )

    async def _persist_shutdown_cancel(self, task_id: str) -> None:
        task = await self._load_task(TaskId(task_id))
        if task is None:
            return
        status = TaskStatus(task.status)
        attempt_id = AttemptId(task.current_attempt_id or f"{task_id}-unknown")
        async with self._session_factory() as session:
            if status is TaskStatus.SCHEDULED:
                with contextlib.suppress(InvalidTransition):
                    await TaskRepo(session, is_legal).transition(
                        TaskId(task_id),
                        [TaskStatus.SCHEDULED],
                        TaskStatus.READY,
                        "engine shutdown before agent start",
                        current_attempt_id=None,
                    )
            elif status in {
                TaskStatus.RUNNING,
                TaskStatus.VALIDATING,
                TaskStatus.INTEGRATING,
            }:
                with contextlib.suppress(InvalidTransition):
                    await TaskRepo(session, is_legal).transition(
                        TaskId(task_id),
                        [status],
                        TaskStatus.RETRY_WAIT,
                        "engine shutdown interrupted attempt",
                        retry_not_before=self._clock(),
                        failure_json={
                            "kind": "infra_error",
                            "summary": "attempt interrupted by orchestrator shutdown",
                            "counts_against_budget": False,
                        },
                    )
            attempt = await AttemptRepo(session).get(str(attempt_id))
            if attempt is not None and attempt.status not in {
                "completed",
                "failed",
                "abandoned",
            }:
                attempt.status = "abandoned"
                attempt.finished_at = self._clock()
            await session.commit()
        await self._destroy_container(str(attempt_id))

    # ================================================================== helpers
    async def _persist_agent_result(
        self, task: TaskModel, attempt_id: AttemptId, result: AgentRunResult
    ) -> None:
        async with self._session_factory() as session:
            attempt = await AttemptRepo(session).get(str(attempt_id))
            if attempt is not None:
                attempt.exit_status = result.exit_status
                attempt.agent_log_path = str(result.log_path) if result.log_path else None
                attempt.token_usage_json = (
                    result.token_usage.model_dump(mode="json") if result.token_usage else None
                )
                attempt.finished_at = self._clock()
            await events.emit_event(
                session,
                project_id=task.project_id,
                task_id=task.id,
                attempt_id=str(attempt_id),
                event_type=events.AGENT_FINISHED,
                payload={
                    "exit_status": result.exit_status,
                    "exit_code": result.exit_code,
                    "duration_seconds": result.duration_seconds,
                    "summary": result.summary or "",
                },
            )
            await self._record_agent_costs(session, task, attempt_id, result)
            await session.commit()

    async def _record_agent_costs(
        self,
        session: AsyncSession,
        task: TaskModel,
        attempt_id: AttemptId,
        result: AgentRunResult,
    ) -> None:
        costs = CostRepo(session)
        now = self._clock()
        if result.token_usage is not None:
            usage = result.token_usage
            amount = usage.total_tokens / 1000.0 * self.config.inference_cost_per_1k_tokens_usd
            await costs.add_cost(
                CostEntryModel(
                    project_id=task.project_id,
                    task_id=task.id,
                    attempt_id=str(attempt_id),
                    kind="inference",
                    amount_usd=amount,
                    units_json=usage.model_dump(mode="json"),
                    note=f"agent:{self.agent.name}",
                )
            )
            await costs.record_usage(
                project_id=task.project_id,
                provider=usage.model or self._provider_key(),
                window_start=usage_window_start(now, self.config.usage_window_seconds),
                tokens=usage.total_tokens,
                requests=1,
            )
        container_seconds = max(0.0, result.duration_seconds)
        await costs.add_cost(
            CostEntryModel(
                project_id=task.project_id,
                task_id=task.id,
                attempt_id=str(attempt_id),
                kind="infra",
                amount_usd=container_seconds * self.config.infra_cost_per_second_usd,
                units_json={"container_seconds": container_seconds},
                note="agent-container",
            )
        )

    async def _persist_validation(
        self,
        attempt_id: AttemptId | str,
        report: ValidationReport,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            attempt = await AttemptRepo(session).get(str(attempt_id))
            if attempt is not None:
                attempt.validation_report_json = report.model_dump(mode="json")
                task_id = task_id or attempt.task_id
                if project_id is None:
                    task = await TaskRepo(session).get(TaskId(attempt.task_id))
                    project_id = task.project_id if task is not None else None
            await events.emit_event(
                session,
                project_id=project_id or "unknown",
                task_id=task_id,
                attempt_id=str(attempt_id),
                event_type=events.VALIDATION_FINISHED,
                payload=report.model_dump(mode="json"),
            )
            await session.commit()

    async def _create_integration(
        self, task: TaskModel, attempt_id: AttemptId, staging_before: str | None, *, status: str
    ) -> str:
        async with self._session_factory() as session:
            integration = IntegrationModel(
                project_id=task.project_id,
                task_id=task.id,
                attempt_id=str(attempt_id),
                status=status,
                staging_commit_before=staging_before,
                conflict_files_json=[],
                at=self._clock(),
            )
            await IntegrationRepo(session).create(integration)
            await events.emit_event(
                session,
                project_id=task.project_id,
                task_id=task.id,
                attempt_id=str(attempt_id),
                event_type=events.INTEGRATION_PREPARED,
                payload={"staging_before": staging_before},
            )
            await session.commit()
            return integration.id

    async def _update_integration(
        self,
        integration_id: str,
        *,
        status: str,
        staging_after: str | None = None,
        conflicts: list[str] | None = None,
        detail: str | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        async def apply(active: AsyncSession) -> None:
            integration = await IntegrationRepo(active).get(integration_id)
            if integration is None:
                return
            integration.status = status
            if staging_after is not None:
                integration.staging_commit_after = staging_after
            if conflicts is not None:
                integration.conflict_files_json = list(conflicts)
            if detail is not None:
                integration.detail = detail

        if session is not None:
            await apply(session)
            return
        async with self._session_factory() as new_session:
            await apply(new_session)
            await new_session.commit()

    async def _mark_attempt_failed(
        self,
        session: AsyncSession,
        attempt_id: AttemptId,
        record: FailureRecord,
        *,
        abandoned: bool,
    ) -> None:
        attempt = await AttemptRepo(session).get(str(attempt_id))
        if attempt is None:
            return
        attempt.status = "abandoned" if abandoned else "failed"
        attempt.failure_json = record.model_dump(mode="json")
        attempt.finished_at = self._clock()

    def _build_agent_request(
        self,
        task: TaskModel,
        workspace: Workspace,
        bundle: Any,
        *,
        attempt_number: int,
        project: ProjectModel,
    ) -> tuple[AgentRunRequest, list[str]]:
        spec = task_spec_from_model(task)
        requested = spec.resources
        effective = self.policy.effective_resources(requested)
        denied: list[str] = []
        if effective != requested:
            denied.append(
                "resource request exceeded policy: "
                f"{requested.model_dump()} -> {effective.model_dump()}"
            )
        network = self.policy.effective_network(
            NetworkPolicy(mode="full" if self.settings.security.allow_network_default else "none")
        )
        timeout = self.policy.effective_timeout(task.timeout_seconds)
        if timeout != task.timeout_seconds:
            denied.append(f"timeout request {task.timeout_seconds}s exceeded policy {timeout}s")
        model = model_config_from_settings(self.settings)
        request = AgentRunRequest(
            task=spec,
            context_bundle=bundle,
            workspace=workspace,
            attempt_number=attempt_number,
            prior_failure_summary=_prior_failure_summary(task),
            resources=effective,
            network=network,
            timeout_seconds=timeout,
            model=model,
            project_id=project.id,
            validation_checks=checks_from_task(task),
        )
        return request, denied

    async def _emit_policy_denied(
        self, task: TaskModel, attempt_id: AttemptId, denied: list[str]
    ) -> None:
        async with self._session_factory() as session:
            await events.emit_event(
                session,
                project_id=task.project_id,
                task_id=task.id,
                attempt_id=str(attempt_id),
                event_type=events.POLICY_DENIED,
                payload={"denied": denied, "action": "requests clamped and ignored"},
            )
            await session.commit()

    def _integration_checks(self, task: TaskModel, spec: Any) -> list[CheckSpec]:
        checks = project_checks(spec)
        return checks or checks_from_task(task)

    async def _next_attempt_number(self, task_id: TaskId) -> int:
        async with self._session_factory() as session:
            return await AttemptRepo(session).count_for_task(str(task_id)) + 1

    async def _load_task(self, task_id: TaskId) -> TaskModel | None:
        async with self._session_factory() as session:
            return await TaskRepo(session).get(task_id)

    async def _load_project(self, project_id: str) -> ProjectModel | None:
        async with self._session_factory() as session:
            return await ProjectRepo(session).get(project_id)

    async def _load_attempt(self, attempt_id: str | None) -> AttemptModel | None:
        if not attempt_id:
            return None
        async with self._session_factory() as session:
            return await AttemptRepo(session).get(attempt_id)

    async def _remove_workspace(self, workspace: Workspace, *, keep: bool) -> None:
        with contextlib.suppress(Exception):
            await self.git.remove_workspace(workspace, keep)

    async def _destroy_container(self, attempt_id: str) -> None:
        if self.sandbox is None:
            return
        with contextlib.suppress(Exception):
            await self.sandbox.destroy(f"ds-{attempt_id}")

    async def _budget_exceeded(
        self, project_id: str, *, session: AsyncSession | None = None
    ) -> bool:
        budget = self.settings.agent.model_budget

        async def check(active: AsyncSession) -> bool:
            tokens, requests = await CostRepo(active).usage_totals(project_id)
            if budget.max_tokens is not None and tokens >= budget.max_tokens:
                return True
            return budget.max_requests is not None and requests >= budget.max_requests

        if session is not None:
            return await check(session)
        async with self._session_factory() as new_session:
            return await check(new_session)

    async def _model_budget(
        self, session: AsyncSession, project_ids: list[str]
    ) -> dict[str, ModelBudgetState]:
        budget = self.settings.agent.model_budget
        if budget.max_tokens is None and budget.max_requests is None:
            return {}
        used_tokens = 0
        used_requests = 0
        costs = CostRepo(session)
        for project_id in project_ids:
            tokens, requests = await costs.usage_totals(project_id)
            used_tokens = max(used_tokens, tokens)
            used_requests = max(used_requests, requests)
        return {
            self._provider_key(): ModelBudgetState(
                remaining_tokens=(
                    None if budget.max_tokens is None else max(0, budget.max_tokens - used_tokens)
                ),
                remaining_requests=(
                    None
                    if budget.max_requests is None
                    else max(0, budget.max_requests - used_requests)
                ),
            )
        }

    def _provider_key(self) -> str:
        return self.settings.agent.default_model or "opencode"

    async def _has_work(self) -> bool:
        if self._running:
            return True
        async with self._session_factory() as session:
            projects = await ProjectRepo(session, is_legal_project).list_by_status(
                [ProjectStatus.EXECUTING, ProjectStatus.PAUSED]
            )
            for project in projects:
                if project.active_plan_id is None:
                    continue
                tasks = await TaskRepo(session).list_for_plan(project.active_plan_id)
                if any(TaskStatus(task.status) not in _TERMINAL_TASK_STATUSES for task in tasks):
                    return True
        return False

    async def drain(self) -> None:
        """Wait for all in-flight attempts (public helper for tests/CLI)."""
        await self._await_running()

    async def _await_running(self) -> None:
        pending = [task for task in self._running.values() if not task.done()]
        if not pending:
            return
        results = await asyncio.gather(*pending, return_exceptions=True)
        for result in results:
            if isinstance(result, SimulatedCrash):
                raise result

    async def _finalize_projects(self) -> None:
        if self.project_service is None:
            return
        async with self._session_factory() as session:
            projects = await ProjectRepo(session, is_legal_project).list_by_status(
                [ProjectStatus.EXECUTING, ProjectStatus.PAUSED]
            )
            project_ids = [project.id for project in projects]
        for project_id in project_ids:
            with contextlib.suppress(Exception):
                await self.project_service.refresh_project_status(project_id)

    async def _recover(self) -> None:
        if self.recovery is None:
            return
        report = await self.recovery.recover()
        for action in decisions_for_report(report):
            runner = asyncio.create_task(
                self._attempt_runner(action.task_id, resume=action),
                name=f"ds-resume-{action.task_id}",
            )
            self._running[action.task_id] = runner
            runner.add_done_callback(lambda _: self._wake.set())

    async def _emit_engine_event(self, event_type: str) -> None:
        async with self._session_factory() as session:
            await events.emit_event(
                session,
                project_id="system",
                event_type=event_type,
                payload={"holder": self.lease_holder, "running": list(self._running)},
            )
            await session.commit()

    async def status(self) -> EngineStatus:
        """Return a point-in-time status snapshot for observability."""
        async with self._session_factory() as session:
            projects = await ProjectRepo(session, is_legal_project).list_by_status(
                [ProjectStatus.EXECUTING, ProjectStatus.PAUSED]
            )
            running: list[str] = []
            queue: list[str] = []
            attempts_total = 0
            budget_usage: dict[str, int] = {}
            for project in projects:
                if project.active_plan_id is None:
                    continue
                tasks = await TaskRepo(session).list_for_plan(project.active_plan_id)
                running.extend(
                    task.id for task in tasks if TaskStatus(task.status) in _ACTIVE_TASK_STATUSES
                )
                queue.extend(
                    task.id for task in tasks if TaskStatus(task.status) is TaskStatus.READY
                )
                attempts_total += await AttemptRepo(session).count_for_project(project.id)
                tokens, requests = await CostRepo(session).usage_totals(project.id)
                budget_usage[project.id] = tokens
                del requests
            lease = await LeaseRepo(session).get(self.config.lease_name)
        return EngineStatus(
            running=running,
            ready_queue=queue,
            deferred=dict(self._deferrals),
            pressure=self._last_pressure,
            budget_usage=budget_usage,
            attempts_total=attempts_total,
            paused=any(
                ProjectStatus(project.status) is ProjectStatus.PAUSED for project in projects
            ),
            lease_holder=lease.holder if lease is not None else None,
            last_round_at=self._last_round_at,
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _task_resources(task: TaskModel) -> Any:
    from ..common.types import ResourceProfile

    return ResourceProfile.model_validate(task.resource_profile_json or {})


def _critical_path_lengths(
    tasks: list[TaskModel], dependencies: dict[str, list[str]]
) -> dict[str, int]:
    """Return a deterministic longest-path length for each task node.

    Edges live in ``task_dependencies`` (not on the ORM row), so callers pass
    the resolved adjacency map.  Cycles are tolerated defensively (a cyclic
    plan is rejected by the planner, but recovery must never loop forever).
    """
    task_ids = {task.id for task in tasks}
    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def depth(task_id: str) -> int:
        if task_id in memo:
            return memo[task_id]
        if task_id in visiting:
            return 0
        visiting.add(task_id)
        longest = 0
        for dependency in dependencies.get(task_id, []):
            if dependency in task_ids:
                longest = max(longest, depth(dependency) + 1)
        visiting.discard(task_id)
        memo[task_id] = longest
        return longest

    return {task.id: depth(task.id) for task in tasks}


def _prior_failure_summary(task: TaskModel) -> str | None:
    failure = task.failure_json or {}
    if not failure:
        return None
    summary = failure.get("summary")
    kind = failure.get("kind")
    if summary:
        return f"{kind}: {summary}" if kind else str(summary)
    return None


def backoff_deadline(now: datetime, attempt_number: int, base: float, maximum: float) -> datetime:
    """Return a capped exponential backoff deadline (no jitter)."""
    delay = min(maximum, base * (2 ** max(0, attempt_number - 1)))
    return now + timedelta(seconds=delay)


__all__ = [
    "FaultInjector",
    "OrchestratorEngine",
    "Replanner",
    "SimulatedCrash",
    "backoff_deadline",
]
