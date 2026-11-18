"""Project lifecycle service: submit, analyze, plan, approve, control, mode.

Every external side effect is preceded by a committed transition (persist before
act).  Each method owns its transaction through the injected async session
factory, so callers cannot accidentally hold a transaction open across planning
or git operations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..common.errors import (
    InvalidTransition,
    PolicyViolation,
    ProjectNotFoundError,
    TaskNotFoundError,
)
from ..common.ids import TaskId, new_project_id
from ..common.types import (
    CheckSpec,
    ProjectMode,
    ProjectStatus,
    RepoHandle,
    RepoProfile,
    TaskStatus,
)
from ..config.policy import SecurityPolicy
from ..config.settings import DevSandboxSettings, get_settings
from ..context.interfaces import ContextManager
from ..db.models import ProjectModel
from ..db.repositories import ProjectRepo, TaskRepo
from ..gitmgr.interfaces import GitManager
from ..planner.interfaces import Planner
from ..planner.models import Plan, PlanStatus
from ..planner.service import PlanService
from ..planner.templates import project_checks_for
from ..spec.models import ProjectSpec, load_spec
from ..validation.interfaces import ValidationReport
from . import events
from .models import OrchestratorConfig
from .state_machine import (
    ModeChangeDirection,
    is_legal,
    is_legal_project,
    mode_change_direction,
)
from .support import repo_handle_for_project, spec_from_project

BaselineRunner = Callable[[RepoHandle, str, list[CheckSpec]], Awaitable[ValidationReport]]


@dataclass(frozen=True)
class TaskCancellation:
    """Outcome of an idempotent task cancellation."""

    task_id: str
    project_id: str
    already_cancelled: bool = False
    blocked_dependents: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TaskPauseState:
    """Outcome of setting a task's reversible pause flag."""

    task_id: str
    project_id: str
    status: str
    paused: bool
    changed: bool = True


class ProjectService:
    """Lifecycle owner for projects (spec §4 steps 1-6 and 15)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        git: GitManager,
        context: ContextManager,
        planner: Planner,
        policy: SecurityPolicy,
        settings: DevSandboxSettings | None = None,
        config: OrchestratorConfig | None = None,
        baseline_runner: BaselineRunner | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.git = git
        self.context = context
        self.planner = planner
        self.policy = policy
        self.settings = settings or get_settings()
        self.config = config or OrchestratorConfig.from_settings(self.settings)
        self.baseline_runner = baseline_runner

    # ------------------------------------------------------------------ submit
    async def submit_project(
        self,
        spec: str | ProjectSpec,
        *,
        project_id: str | None = None,
        spec_yaml: str | None = None,
    ) -> ProjectModel:
        """Validate and persist a project in ``LOADED``."""
        parsed = spec if isinstance(spec, ProjectSpec) else load_spec(spec)
        pid = project_id or new_project_id()
        async with self._session_factory() as session:
            project = ProjectModel(
                id=pid,
                name=parsed.name,
                mode=parsed.mode.value,
                status=ProjectStatus.LOADED.value,
                spec_yaml=spec_yaml or (spec if isinstance(spec, str) else None),
                spec_hash=parsed.spec_hash,
                spec_json=parsed.model_dump_json(),
                repo_source_json=parsed.repo.model_dump_json(),
                main_branch=parsed.repo.ref,
                staging_branch=f"devsandbox/{pid}/staging",
            )
            await ProjectRepo(session, is_legal_project).create(project)
            await events.emit_event(
                session,
                project_id=pid,
                event_type=events.PROJECT_CREATED,
                payload={
                    "name": parsed.name,
                    "mode": parsed.mode.value,
                    "spec_hash": parsed.spec_hash,
                },
            )
            await session.commit()
            return project

    # ----------------------------------------------------------------- analyze
    async def analyze(self, project_id: str) -> RepoProfile:
        """Initialize the canonical repo, run analysis, and (optionally) baseline checks."""
        project = await self._transition_project(
            project_id,
            [ProjectStatus.LOADED],
            ProjectStatus.ANALYZING,
            "analysis started",
        )
        try:
            spec = spec_from_project(project)
            repo = await self.git.init_project_repo(project_id, spec.repo)
            revision = repo.base_commit or repo.ref
            async with self._session_factory() as session:
                stored = await ProjectRepo(session, is_legal_project).get(project_id)
                if stored is not None:
                    stored.repo_path = repo.path
                await events.emit_event(
                    session,
                    project_id=project_id,
                    event_type=events.PROJECT_ANALYZED,
                    payload={
                        "repo_path": repo.path,
                        "revision": revision,
                        "staging_branch": repo.staging_branch,
                    },
                )
                await session.commit()

            profile = await self.context.analyze_repo(
                repo, revision, validation_commands=spec.validation.commands
            )

            if self.baseline_runner is not None:
                report = await self.baseline_runner(
                    repo, revision, project_checks_for(spec, profile)
                )
                async with self._session_factory() as session:
                    await events.emit_event(
                        session,
                        project_id=project_id,
                        event_type="validation.baseline",
                        payload=report.model_dump(mode="json"),
                    )
                    await session.commit()
            return profile
        except Exception:
            await self._fail_project(project_id, "analysis failed")
            raise

    # -------------------------------------------------------------------- plan
    async def plan(self, project_id: str) -> Plan:
        """Run the planner, activate (or queue for approval) the resulting plan."""
        project = await self._transition_project(
            project_id,
            [ProjectStatus.ANALYZING],
            ProjectStatus.PLANNING,
            "planning started",
        )
        try:
            async with self._session_factory() as session:
                service = self._plan_service(session)
                plan = await service.create_plan(project_id)
                await session.commit()
        except Exception:
            await self._fail_project(project_id, "planning failed")
            raise

        if plan.status is PlanStatus.REJECTED:
            await self._fail_project(project_id, "planner produced an invalid plan")
            return plan

        target = (
            ProjectStatus.EXECUTING
            if project.mode == ProjectMode.AUTONOMOUS.value
            else ProjectStatus.AWAITING_APPROVAL
        )
        await self._transition_project(
            project_id,
            [ProjectStatus.PLANNING],
            target,
            f"plan v{plan.version} ready",
            active_plan_id=plan.id,
        )
        async with self._session_factory() as session:
            await events.emit_event(
                session,
                project_id=project_id,
                event_type=events.PROJECT_PLANNED,
                payload={
                    "plan_id": plan.id,
                    "version": plan.version,
                    "status": plan.status.value,
                    "tasks": len(plan.tasks),
                },
            )
            await session.commit()
        return plan

    # ----------------------------------------------------------------- approve
    async def approve_plan(self, project_id: str, plan_id: str, *, approver: str = "user") -> Plan:
        """Approve a proposed plan and move the project into ``EXECUTING``."""
        async with self._session_factory() as session:
            project = await ProjectRepo(session, is_legal_project).get(project_id)
            if project is None:
                raise ProjectNotFoundError(f"project {project_id} not found")
            if ProjectStatus(project.status) is not ProjectStatus.AWAITING_APPROVAL:
                raise InvalidTransition(
                    f"project {project_id} is {project.status}, not awaiting approval"
                )
            service = self._plan_service(session)
            plan = await service.approve_plan(plan_id, approver=approver)
            await session.commit()
        await self._transition_project(
            project_id,
            [ProjectStatus.AWAITING_APPROVAL],
            ProjectStatus.EXECUTING,
            f"plan {plan_id} approved by {approver}",
            active_plan_id=plan_id,
        )
        async with self._session_factory() as session:
            await events.emit_event(
                session,
                project_id=project_id,
                event_type=events.PROJECT_APPROVED,
                payload={"plan_id": plan_id, "approver": approver},
            )
            await session.commit()
        return plan

    # ----------------------------------------------------------------- reject
    async def reject_plan(
        self,
        project_id: str,
        plan_id: str,
        *,
        reason: str = "rejected by user",
        rejected_by: str = "user",
    ) -> Plan:
        """Reject a proposed plan without changing the project state.

        The project remains in ``AWAITING_APPROVAL`` so the operator can submit
        a new plan version, cancel, or edit policy-compliant inputs.  Execution
        can never start from a rejected plan because only active plans are
        approved.
        """
        async with self._session_factory() as session:
            project = await ProjectRepo(session, is_legal_project).get(project_id)
            if project is None:
                raise ProjectNotFoundError(f"project {project_id} not found")
            if ProjectStatus(project.status) is not ProjectStatus.AWAITING_APPROVAL:
                raise InvalidTransition(
                    f"project {project_id} is {project.status}, not awaiting approval"
                )
            service = self._plan_service(session)
            plan = await service.reject_plan(plan_id, reason)
            if plan.project_id not in {"", project_id}:
                raise InvalidTransition(f"plan {plan_id} does not belong to project {project_id}")
            await session.commit()
        await self._event(
            project_id,
            "plan.rejected",
            payload={"plan_id": plan_id, "reason": reason, "rejected_by": rejected_by},
        )
        return plan

    # --------------------------------------------------------------- controls
    async def start(self, project_id: str) -> ProjectModel:
        """Start/resume execution (``PAUSED`` -> ``EXECUTING``; idempotent)."""
        project = await self._get_project(project_id)
        if ProjectStatus(project.status) is ProjectStatus.EXECUTING:
            return project
        if ProjectStatus(project.status) is not ProjectStatus.PAUSED:
            raise InvalidTransition(
                f"project {project_id} is {project.status}; "
                "approve the plan first or resume a paused project"
            )
        await self._transition_project(
            project_id, [ProjectStatus.PAUSED], ProjectStatus.EXECUTING, "started by user"
        )
        await self._event(project_id, events.PROJECT_STARTED)
        return await self._get_project(project_id)

    async def pause(self, project_id: str) -> ProjectModel:
        """Pause execution (``EXECUTING`` -> ``PAUSED``; idempotent)."""
        project = await self._get_project(project_id)
        if ProjectStatus(project.status) is ProjectStatus.PAUSED:
            return project
        await self._transition_project(
            project_id, [ProjectStatus.EXECUTING], ProjectStatus.PAUSED, "paused by user"
        )
        await self._event(project_id, events.PROJECT_PAUSED)
        return await self._get_project(project_id)

    async def resume(self, project_id: str) -> ProjectModel:
        """Resume a paused project (idempotent; only emits when it was paused)."""
        project = await self._get_project(project_id)
        if ProjectStatus(project.status) is not ProjectStatus.PAUSED:
            return project
        resumed = await self.resume_if_paused(project_id)
        await self._event(project_id, events.PROJECT_RESUMED)
        return resumed

    async def resume_if_paused(self, project_id: str) -> ProjectModel:
        """Translate ``PAUSED -> EXECUTING`` without emitting the user event."""
        project = await self._get_project(project_id)
        if ProjectStatus(project.status) is not ProjectStatus.PAUSED:
            return project
        await self._transition_project(
            project_id, [ProjectStatus.PAUSED], ProjectStatus.EXECUTING, "resumed"
        )
        return await self._get_project(project_id)

    async def cancel_task(self, task_id: str, *, requested_by: str = "user") -> TaskCancellation:
        """Cancel one task and block its dependents.

        Cancellation is idempotent for an already-cancelled task.  Terminal
        Completed/failed tasks cannot be rewritten, and BLOCKED has no legal
        edge to CANCELLED in the authoritative state machine; those return a
        clear conflict instead of being silently changed.
        """
        for _attempt in range(4):
            async with self._session_factory() as session:
                task = await TaskRepo(session).get(TaskId(task_id))
                if task is None:
                    raise TaskNotFoundError(f"task {task_id} not found")
                status = TaskStatus(task.status)
                if status is TaskStatus.CANCELLED:
                    return TaskCancellation(
                        task_id=task_id,
                        project_id=task.project_id,
                        already_cancelled=True,
                    )
                if status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.BLOCKED,
                }:
                    raise InvalidTransition(
                        f"task {task_id} is {status.value} and cannot be cancelled"
                    )
                tasks = TaskRepo(session, is_legal)
                try:
                    await tasks.transition(
                        TaskId(task_id),
                        [status],
                        TaskStatus.CANCELLED,
                        f"cancelled by {requested_by}",
                        retry_not_before=None,
                        paused=False,
                    )
                except InvalidTransition:
                    # The task advanced between load and CAS; retry from the
                    # new state instead of reporting a spurious conflict.
                    await session.rollback()
                    continue
                blocked = await tasks.mark_dependents_blocked(
                    task_id, reason=f"dependency {task.key} cancelled"
                )
                await events.emit_event(
                    session,
                    project_id=task.project_id,
                    task_id=task_id,
                    event_type=events.TASK_CANCELLED,
                    payload={
                        "requested_by": requested_by,
                        "blocked_dependents": [row.id for row in blocked],
                    },
                )
                if blocked:
                    await events.emit_event(
                        session,
                        project_id=task.project_id,
                        task_id=task_id,
                        event_type=events.DEPENDENTS_BLOCKED,
                        payload={"blocked": [row.id for row in blocked]},
                    )
                await session.commit()
                return TaskCancellation(
                    task_id=task_id,
                    project_id=task.project_id,
                    blocked_dependents=[row.id for row in blocked],
                )
        raise InvalidTransition(f"task {task_id} kept changing; retry cancellation")

    async def set_task_paused(
        self,
        task_id: str,
        paused: bool,
        *,
        requested_by: str = "user",
    ) -> TaskPauseState:
        """Set/clear the reversible task hold.

        Only queued states (``PENDING``, ``READY``, ``RETRY_WAIT``) can be
        paused.  Active attempts are not resumable, so pausing one would lose
        agent work; cancel the attempt or pause the project instead.
        """
        async with self._session_factory() as session:
            repo = TaskRepo(session)
            task = await repo.get(TaskId(task_id))
            if task is None:
                raise TaskNotFoundError(f"task {task_id} not found")
            status = TaskStatus(task.status)
            if task.paused is paused:
                return TaskPauseState(
                    task_id=task_id,
                    project_id=task.project_id,
                    status=status.value,
                    paused=paused,
                    changed=False,
                )
            pausable = [TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RETRY_WAIT]
            if status not in pausable:
                raise InvalidTransition(self._task_hold_error(task_id, status, paused))
            changed = await repo.set_paused(TaskId(task_id), paused, from_states=pausable)
            if not changed:
                # A concurrent transition or hold changed the row between load
                # and CAS; report the current state instead of blindly writing.
                latest = await repo.get(TaskId(task_id))
                if latest is None:
                    raise TaskNotFoundError(f"task {task_id} not found")
                latest_status = TaskStatus(latest.status)
                if latest.paused is paused:
                    return TaskPauseState(
                        task_id=task_id,
                        project_id=latest.project_id,
                        status=latest_status.value,
                        paused=paused,
                        changed=False,
                    )
                if latest_status not in pausable:
                    raise InvalidTransition(self._task_hold_error(task_id, latest_status, paused))
                raise InvalidTransition(f"task {task_id} kept changing; retry the hold")
            await events.emit_event(
                session,
                project_id=task.project_id,
                task_id=task_id,
                event_type=events.TASK_PAUSED if paused else events.TASK_RESUMED,
                payload={"requested_by": requested_by},
            )
            await session.commit()
            return TaskPauseState(
                task_id=task_id,
                project_id=task.project_id,
                status=status.value,
                paused=paused,
            )

    @staticmethod
    def _task_hold_error(task_id: str, status: TaskStatus, paused: bool) -> str:
        action = "paused" if paused else "resumed"
        return (
            f"task {task_id} is {status.value} and cannot be {action}; "
            "cancel an active attempt or pause the project instead"
        )

    async def pause_task(self, task_id: str, *, requested_by: str = "user") -> TaskPauseState:
        """Pause a queued task (idempotent)."""
        return await self.set_task_paused(task_id, True, requested_by=requested_by)

    async def resume_task(self, task_id: str, *, requested_by: str = "user") -> TaskPauseState:
        """Resume a paused task (idempotent)."""
        return await self.set_task_paused(task_id, False, requested_by=requested_by)

    async def cancel(self, project_id: str) -> ProjectModel:
        """Cancel a project and all of its cancellable tasks.

        Scheduling is stopped first (EXECUTING -> PAUSED) so a scheduler round
        cannot launch a task while cancellation is in progress.  The caller
        (the API/orchestrator process that owns the lease) is responsible for
        aborting any in-flight attempt after this returns; the engine exposes
        ``cancel_running_project`` for exactly that.
        """
        project = await self._get_project(project_id)
        status = ProjectStatus(project.status)
        if status is ProjectStatus.CANCELLED:
            return project
        if status in {ProjectStatus.COMPLETED, ProjectStatus.FAILED}:
            raise InvalidTransition(
                f"project {project_id} is {status.value} and cannot be cancelled"
            )
        if status is ProjectStatus.EXECUTING:
            await self._transition_project(
                project_id,
                [ProjectStatus.EXECUTING],
                ProjectStatus.PAUSED,
                "cancel requested",
            )
            status = ProjectStatus.PAUSED

        async with self._session_factory() as session:
            task_rows = await TaskRepo(session).list_for_project(project_id)
            task_ids = [task.id for task in task_rows]
        for task_id in task_ids:
            try:
                await self.cancel_task(task_id, requested_by="project cancellation")
            except (InvalidTransition, TaskNotFoundError):
                # Terminal/blocked tasks cannot be cancelled and a concurrent
                # completion may have removed the row; neither blocks the
                # project-level cancellation.
                continue

        await self._transition_project(
            project_id,
            [status],
            ProjectStatus.CANCELLED,
            "cancelled by user",
        )
        await self._event(project_id, events.PROJECT_CANCELLED)
        return await self._get_project(project_id)

    # -------------------------------------------------------------------- mode
    async def set_mode(
        self,
        project_id: str,
        mode: ProjectMode | str,
        *,
        requested_by: str = "user",
    ) -> ProjectModel:
        """Change operating mode.

        ``autonomous -> supervised`` is always allowed (privilege downgrade).
        ``supervised -> autonomous`` is an upgrade and requires an explicit
        user/API caller; planners and agents are rejected.
        """
        target = ProjectMode(mode)
        project = await self._get_project(project_id)
        direction = mode_change_direction(project.mode, target.value)
        if direction is ModeChangeDirection.NONE:
            return project
        if direction is ModeChangeDirection.UPGRADE and requested_by not in {"user", "api"}:
            raise PolicyViolation(
                "autonomous mode upgrade requires an explicit user/API call; "
                f"requested by {requested_by!r}"
            )
        if direction is ModeChangeDirection.UPGRADE and not self.policy.autonomous_mode_allowed():
            raise PolicyViolation("autonomous mode is disabled by security policy")
        async with self._session_factory() as session:
            stored = await ProjectRepo(session, is_legal_project).get(project_id)
            if stored is None:
                raise ProjectNotFoundError(f"project {project_id} not found")
            previous = stored.mode
            stored.mode = target.value
            await events.emit_event(
                session,
                project_id=project_id,
                event_type=events.PROJECT_MODE_CHANGED,
                payload={
                    "from": previous,
                    "to": target.value,
                    "direction": direction.value,
                    "requested_by": requested_by,
                },
            )
            await session.commit()
        return await self._get_project(project_id)

    # --------------------------------------------------------------- derivation
    async def refresh_project_status(self, project_id: str) -> ProjectStatus:
        """Derive and persist a terminal project status when all tasks finish."""
        from .state_machine import derive_project_status

        async with self._session_factory() as session:
            project = await ProjectRepo(session, is_legal_project).get(project_id)
            if project is None:
                raise ProjectNotFoundError(f"project {project_id} not found")
            current = ProjectStatus(project.status)
            if current not in {ProjectStatus.EXECUTING, ProjectStatus.PAUSED}:
                return current
            tasks = await TaskRepo(session).list_for_project(
                project_id, plan_id=project.active_plan_id
            )
            derived = derive_project_status(tasks)
            if derived is ProjectStatus.EXECUTING:
                return current
            try:
                await ProjectRepo(session, is_legal_project).transition(
                    project_id,
                    [current],
                    derived,
                    f"derived from {len(tasks)} task(s)",
                )
            except InvalidTransition:
                return current
            await session.commit()
        await self._event(
            project_id,
            events.PROJECT_COMPLETED
            if derived is ProjectStatus.COMPLETED
            else events.PROJECT_FAILED,
            payload={"derived_status": derived.value},
        )
        return derived

    async def needs_attention(self, project_id: str, reason: str) -> None:
        """Surface a supervised-mode "needs attention" signal."""
        async with self._session_factory() as session:
            await events.emit_event(
                session,
                project_id=project_id,
                event_type=events.PROJECT_NEEDS_ATTENTION,
                payload={"reason": reason},
            )
            await session.commit()

    # ----------------------------------------------------------------- helpers
    async def repo_handle(self, project_id: str) -> RepoHandle:
        """Return the canonical repo handle for a project."""
        return repo_handle_for_project(await self._get_project(project_id))

    async def spec(self, project_id: str) -> ProjectSpec:
        """Return the stored project spec."""
        return spec_from_project(await self._get_project(project_id))

    def _plan_service(self, session: AsyncSession) -> PlanService:
        return PlanService(
            session,
            self.planner,
            self.policy,
            context_manager=self.context,
            settings=self.settings,
            auto_approve=True,
        )

    async def _get_project(self, project_id: str) -> ProjectModel:
        async with self._session_factory() as session:
            project = await ProjectRepo(session, is_legal_project).get(project_id)
            if project is None:
                raise ProjectNotFoundError(f"project {project_id} not found")
            return project

    async def _transition_project(
        self,
        project_id: str,
        from_states: list[ProjectStatus],
        to_state: ProjectStatus,
        reason: str,
        *,
        allow_same: bool = False,
        **fields: Any,
    ) -> ProjectModel:
        async with self._session_factory() as session:
            repo = ProjectRepo(session, is_legal_project)
            project = await repo.get(project_id)
            if project is None:
                raise ProjectNotFoundError(f"project {project_id} not found")
            if allow_same and ProjectStatus(project.status) is to_state:
                return project
            updated = await repo.transition(project_id, from_states, to_state, reason, **fields)
            await session.commit()
            return updated

    async def _fail_project(self, project_id: str, reason: str) -> None:
        async with self._session_factory() as session:
            repo = ProjectRepo(session, is_legal_project)
            project = await repo.get(project_id)
            if project is None:
                return
            current = ProjectStatus(project.status)
            try:
                await repo.transition(project_id, [current], ProjectStatus.FAILED, reason)
                await session.commit()
            except InvalidTransition:
                return
        await self._event(project_id, events.PROJECT_FAILED, payload={"reason": reason})

    async def _event(
        self, project_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
        async with self._session_factory() as session:
            await events.emit_event(
                session, project_id=project_id, event_type=event_type, payload=payload
            )
            await session.commit()


__all__ = ["BaselineRunner", "ProjectService", "TaskCancellation", "TaskPauseState"]
