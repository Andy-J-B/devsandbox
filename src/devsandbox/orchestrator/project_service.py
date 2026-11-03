"""Project lifecycle service: submit, analyze, plan, approve, control, mode.

Every external side effect is preceded by a committed transition (persist before
act).  Each method owns its transaction through the injected async session
factory, so callers cannot accidentally hold a transaction open across planning
or git operations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..common.errors import InvalidTransition, PolicyViolation, ProjectNotFoundError
from ..common.ids import TaskId, new_project_id
from ..common.types import (
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
from ..spec.models import ProjectSpec, load_spec
from ..validation.interfaces import ValidationReport
from . import events
from .models import OrchestratorConfig
from .state_machine import (
    ModeChangeDirection,
    is_legal_project,
    mode_change_direction,
)
from .support import repo_handle_for_project, spec_from_project

BaselineRunner = Callable[[RepoHandle, str], Awaitable[ValidationReport]]


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

            profile = await self.context.analyze_repo(repo, revision)

            if self.baseline_runner is not None:
                report = await self.baseline_runner(repo, revision)
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
        """Resume a paused project."""
        project = await self.resume_if_paused(project_id)
        await self._event(project_id, events.PROJECT_RESUMED)
        return project

    async def resume_if_paused(self, project_id: str) -> ProjectModel:
        """Translate ``PAUSED -> EXECUTING`` without emitting the user event."""
        project = await self._get_project(project_id)
        if ProjectStatus(project.status) is not ProjectStatus.PAUSED:
            return project
        await self._transition_project(
            project_id, [ProjectStatus.PAUSED], ProjectStatus.EXECUTING, "resumed"
        )
        return await self._get_project(project_id)

    async def cancel(self, project_id: str) -> ProjectModel:
        """Cancel a project and all of its non-terminal tasks."""
        project = await self._get_project(project_id)
        status = ProjectStatus(project.status)
        if status is ProjectStatus.CANCELLED:
            return project
        async with self._session_factory() as session:
            tasks = TaskRepo(session)
            for task in await tasks.list_for_project(project_id):
                if TaskStatus(task.status) in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.BLOCKED,
                    TaskStatus.CANCELLED,
                }:
                    continue
                try:
                    await tasks.transition(
                        TaskId(task.id),
                        [TaskStatus(task.status)],
                        TaskStatus.CANCELLED,
                        "project cancelled",
                    )
                except InvalidTransition:
                    continue
            await session.commit()
        await self._transition_project(
            project_id,
            [
                ProjectStatus.LOADED,
                ProjectStatus.ANALYZING,
                ProjectStatus.PLANNING,
                ProjectStatus.AWAITING_APPROVAL,
                ProjectStatus.EXECUTING,
                ProjectStatus.PAUSED,
            ],
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


__all__ = ["BaselineRunner", "ProjectService"]
