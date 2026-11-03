"""Crash recovery: reconcile persisted orchestration state with the world.

Recovery runs at startup, after the orchestrator lease is acquired and before
the execution loop.  Every action is derived from *persisted* state plus
observations (Docker containers, git workspaces, integration rows) and is
idempotent: crashing during recovery and rerunning it converges on the same
outcome.

The full decision table lives in ``docs/recovery.md``.  The implementation
below intentionally uses the conservative MVP policy for a still-running
container (kill + retry as an infra failure, which does not consume the task's
retry budget) instead of re-attaching to it.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..common.ids import TaskId
from ..common.logging import get_logger
from ..common.time import utcnow
from ..common.types import ProjectStatus, TaskStatus
from ..db.models import AttemptModel, ProjectModel, TaskModel
from ..db.repositories import AttemptRepo, IntegrationRepo, ProjectRepo, TaskRepo
from ..gitmgr.interfaces import GitManager, Workspace
from ..sandbox.interfaces import ManagedContainer, SandboxManager
from . import events
from .models import (
    OrchestratorConfig,
    RecoveryAction,
    RecoveryActionKind,
    RecoveryReport,
)
from .state_machine import is_legal, is_legal_project
from .support import repo_handle_for_project

_log = get_logger("devsandbox.orchestrator.recovery")

_ACTIVE_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.SCHEDULED,
        TaskStatus.RUNNING,
        TaskStatus.VALIDATING,
        TaskStatus.VALIDATED,
        TaskStatus.INTEGRATING,
    }
)


class RecoveryService:
    """Reconcile non-terminal tasks/attempts after a process restart."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        git: GitManager,
        sandbox: SandboxManager | None = None,
        config: OrchestratorConfig | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self.git = git
        self.sandbox = sandbox
        self.config = config or OrchestratorConfig()
        self._clock = clock

    # ------------------------------------------------------------------ entry
    async def recover(self) -> RecoveryReport:
        """Run the reconciliation pass and emit a ``RECOVERY_COMPLETED`` event."""
        started = self._clock()
        report = RecoveryReport(started_at=started)
        seen_tasks: set[str] = set()
        active_attempts: dict[str, AttemptModel] = {}

        async with self._session_factory() as session:
            tasks = await TaskRepo(session, is_legal).list_by_status(
                sorted(_ACTIVE_TASK_STATUSES, key=lambda status: status.value)
            )
            for active_attempt in await AttemptRepo(session).list_active():
                active_attempts[active_attempt.id] = active_attempt
            for task in tasks:
                seen_tasks.add(task.id)
                project = await ProjectRepo(session, is_legal_project).get(task.project_id)
                attempt = await self._attempt_for_task(session, task, active_attempts)
                action = await self._reconcile_task(session, task, project, attempt)
                if action is not None:
                    report.actions.append(action)
                    await events.emit_event(
                        session,
                        project_id=task.project_id,
                        task_id=task.id,
                        attempt_id=action.attempt_id,
                        event_type=events.RECOVERY_ACTION,
                        payload={
                            "kind": action.kind.value,
                            "detail": action.detail,
                        },
                    )
            # Active attempts whose task is not in an active state are orphans.
            for attempt in active_attempts.values():
                if attempt.task_id in seen_tasks:
                    continue
                await self._abandon_attempt(session, attempt)
                report.actions.append(
                    RecoveryAction(
                        kind=RecoveryActionKind.NONE,
                        task_id=attempt.task_id,
                        attempt_id=attempt.id,
                        detail="orphan attempt abandoned",
                    )
                )
            await session.commit()

        report.containers_destroyed.extend(await self.reap_orphans())
        report.workspaces_removed.extend(await self._clean_orphan_workspaces())
        report.finished_at = self._clock()
        async with self._session_factory() as session:
            await events.emit_event(
                session,
                project_id="system",
                event_type=events.RECOVERY_COMPLETED,
                payload={
                    "actions": [
                        {
                            "kind": action.kind.value,
                            "task_id": action.task_id,
                            "attempt_id": action.attempt_id,
                            "detail": action.detail,
                        }
                        for action in report.actions
                    ],
                    "summary": report.summary,
                    "containers_destroyed": report.containers_destroyed,
                    "workspaces_removed": report.workspaces_removed,
                },
            )
            await session.commit()
        return report

    # -------------------------------------------------------------- decision
    async def _reconcile_task(
        self,
        session: AsyncSession,
        task: TaskModel,
        project: ProjectModel | None,
        attempt: AttemptModel | None,
    ) -> RecoveryAction | None:
        status = TaskStatus(task.status)
        if status not in _ACTIVE_TASK_STATUSES:
            return None

        # A terminal/paused-away project must not resume work on restart.
        if project is None or ProjectStatus(project.status) not in {
            ProjectStatus.EXECUTING,
            ProjectStatus.PAUSED,
        }:
            if attempt is not None:
                await self._abandon_attempt(session, attempt)
                await self._destroy_container(attempt)
            return RecoveryAction(
                kind=RecoveryActionKind.NONE,
                task_id=task.id,
                attempt_id=attempt.id if attempt else None,
                detail="project is not executing",
            )

        container = await self._observe_container(attempt)
        repo = repo_handle_for_project(project)

        if status is TaskStatus.SCHEDULED:
            # Row 1: scheduled, no agent work has happened.
            await self._destroy_container(attempt, container)
            if attempt is not None:
                await self._abandon_attempt(session, attempt)
            await self._transition(
                session,
                task,
                TaskStatus.SCHEDULED,
                TaskStatus.READY,
                "recovery: scheduled attempt never started",
                current_attempt_id=None,
                retry_not_before=None,
            )
            return self._action(
                RecoveryActionKind.REVERT_READY, task, attempt, "scheduled with no container"
            )

        if status is TaskStatus.RUNNING:
            # Row 2: still-running container. MVP policy: kill + retry as infra.
            if container is not None and container.state.lower() == "running":
                await self._destroy_container(attempt, container)
                if attempt is not None:
                    await self._abandon_attempt(session, attempt)
                await self._retry_infra(session, task, "running container killed on recovery")
                return self._action(
                    RecoveryActionKind.KILL_AND_RETRY,
                    task,
                    attempt,
                    "running container killed; retrying as infra failure",
                )
            # Rows 3/4: exited container or recoverable workspace -> collect.
            if container is not None and container.state.lower() in {
                "exited",
                "dead",
                "created",
            }:
                return self._action(
                    RecoveryActionKind.RESUME_COLLECT,
                    task,
                    attempt,
                    "container finished; continuing at collect_changes",
                )
            if await self._workspace_has_changes(attempt):
                return self._action(
                    RecoveryActionKind.RESUME_COLLECT,
                    task,
                    attempt,
                    "workspace has changes; continuing at collect_changes",
                )
            # Row 5: nothing recoverable.
            await self._destroy_container(attempt, container)
            if attempt is not None:
                await self._abandon_attempt(session, attempt)
            await self._retry_infra(session, task, "nothing recoverable after crash")
            return self._action(
                RecoveryActionKind.RETRY_INFRA,
                task,
                attempt,
                "nothing recoverable; retrying as infra failure",
            )

        if status is TaskStatus.VALIDATING:
            report = attempt.validation_report_json if attempt is not None else None
            if report is None:
                return self._action(
                    RecoveryActionKind.RERUN_VALIDATION,
                    task,
                    attempt,
                    "validation report missing; rerunning validation",
                )
            return self._action(
                RecoveryActionKind.RESUME_VALIDATION_DECISION,
                task,
                attempt,
                "validation report persisted; continuing decision",
            )

        if status is TaskStatus.VALIDATED:
            return self._action(
                RecoveryActionKind.RESUME_INTEGRATION,
                task,
                attempt,
                "validation finished; restarting integration",
            )

        # INTEGRATING -----------------------------------------------------
        if attempt is None:
            await self._retry_infra(session, task, "integration attempt missing")
            return self._action(
                RecoveryActionKind.RETRY_INFRA,
                task,
                attempt,
                "integration attempt missing",
            )
        integration = await IntegrationRepo(session).latest_for_attempt(attempt.id)
        head = await self.git.staging_head(repo)
        if integration is not None and integration.staging_commit_after is not None:
            candidate = integration.staging_commit_after
            if await self._contains_commit(repo, candidate, head):
                await self._mark_completed(
                    session, task, attempt, integration.id, head or candidate
                )
                return self._action(
                    RecoveryActionKind.MARK_COMPLETED,
                    task,
                    attempt,
                    "staging already contains the attempt commit",
                )
            if (
                integration.staging_commit_before is not None
                and head is not None
                and head != integration.staging_commit_before
            ):
                # Row 10: staging moved but the candidate is not recorded as
                # integrated. Roll back to the recorded pre-integration head and
                # retry the (idempotent) integration step.
                with contextlib.suppress(Exception):
                    await self.git.reset_staging_to(repo, integration.staging_commit_before)
                await self._abort_candidate(repo)
                return self._action(
                    RecoveryActionKind.RESET_AND_RETRY,
                    task,
                    attempt,
                    "staging advanced without a committed integration record; reset + retry",
                )
        # Row 8: a prepared candidate (or no row at all) was never committed.
        await self._abort_candidate(repo)
        return self._action(
            RecoveryActionKind.RESET_AND_RETRY,
            task,
            attempt,
            "integration interrupted before commit; restarting integration",
        )

    # -------------------------------------------------------------- mutations
    async def _transition(
        self,
        session: AsyncSession,
        task: TaskModel,
        from_state: TaskStatus,
        to_state: TaskStatus,
        reason: str,
        **fields: Any,
    ) -> None:
        with contextlib.suppress(Exception):
            await TaskRepo(session, is_legal).transition(
                TaskId(task.id),
                [from_state],
                to_state,
                reason,
                **fields,
            )

    async def _retry_infra(self, session: AsyncSession, task: TaskModel, reason: str) -> None:
        await self._transition(
            session,
            task,
            TaskStatus(task.status),
            TaskStatus.RETRY_WAIT,
            f"recovery: {reason}",
            retry_not_before=self._clock(),
            failure_json={
                "kind": "infra_error",
                "summary": reason,
                "counts_against_budget": False,
                "infra_failures": int((task.failure_json or {}).get("infra_failures", 0)) + 1,
            },
        )

    async def _mark_completed(
        self,
        session: AsyncSession,
        task: TaskModel,
        attempt: AttemptModel,
        integration_id: str,
        staging_commit: str,
    ) -> None:
        integration = await IntegrationRepo(session).get(integration_id)
        if integration is not None:
            integration.status = "integrated"
            integration.staging_commit_after = staging_commit
        attempt.status = "completed"
        attempt.finished_at = self._clock()
        await self._transition(
            session,
            task,
            TaskStatus.INTEGRATING,
            TaskStatus.COMPLETED,
            "recovery: integration already committed",
            result_json={"staging_commit": staging_commit, "recovered": True},
        )
        await events.emit_event(
            session,
            project_id=task.project_id,
            task_id=task.id,
            attempt_id=attempt.id,
            event_type=events.INTEGRATION_FINISHED,
            payload={"status": "integrated", "staging_commit": staging_commit, "recovered": True},
        )

    async def _abandon_attempt(self, session: AsyncSession, attempt: AttemptModel) -> None:
        if attempt.status in {"completed", "failed", "abandoned", "cancelled"}:
            return
        attempt.status = "abandoned"
        attempt.finished_at = self._clock()
        await events.emit_event(
            session,
            project_id="system",
            task_id=attempt.task_id,
            attempt_id=attempt.id,
            event_type=events.ATTEMPT_ABANDONED,
            payload={"reason": "recovery"},
        )

    # ----------------------------------------------------------- observations
    async def _attempt_for_task(
        self,
        session: AsyncSession,
        task: TaskModel,
        active_attempts: dict[str, AttemptModel],
    ) -> AttemptModel | None:
        if task.current_attempt_id:
            attempt = active_attempts.get(task.current_attempt_id)
            if attempt is not None:
                return attempt
            attempt = await AttemptRepo(session).get(task.current_attempt_id)
            if attempt is not None and attempt.status not in {
                "completed",
                "failed",
                "abandoned",
                "cancelled",
            }:
                return attempt
        return await AttemptRepo(session).latest_for_task(task.id)

    async def _observe_container(self, attempt: AttemptModel | None) -> ManagedContainer | None:
        if attempt is None or self.sandbox is None:
            return None
        container_id = attempt.container_id or f"ds-{attempt.id}"
        found = await self.sandbox.inspect(container_id)
        if found is not None:
            return found
        with contextlib.suppress(Exception):
            for container in await self.sandbox.list_managed():
                if (
                    container.container_id == container_id
                    or container.labels.get("devsandbox.attempt") == attempt.id
                ):
                    return container
        return None

    async def _workspace_has_changes(self, attempt: AttemptModel | None) -> bool:
        if attempt is None:
            return False
        if attempt.head_commit:
            return True
        if not attempt.workspace_path:
            return False
        if not Path(attempt.workspace_path).exists():
            return False
        workspace = Workspace(
            attempt_id=attempt.id,  # type: ignore[arg-type]
            path=attempt.workspace_path,
            base_commit=attempt.base_commit or "",
            branch=attempt.branch or "",
        )
        with contextlib.suppress(Exception):
            changes = await self.git.collect_changes(workspace)
            return bool(changes.head_commit or changes.commits or changes.files_changed)
        return False

    async def _contains_commit(self, repo: Any, ancestor: str, head: str | None) -> bool:
        """Return whether ``head`` contains ``ancestor`` (ancestry check)."""
        if head is None:
            return False
        if head == ancestor:
            return True
        checker = getattr(self.git, "is_ancestor", None)
        if callable(checker):
            with contextlib.suppress(Exception):
                return bool(await checker(repo, ancestor, head))
        return False

    async def _abort_candidate(self, repo: Any) -> None:
        detect = getattr(self.git, "detect_interrupted_integration", None)
        abort = getattr(self.git, "abort_integration", None)
        if callable(detect) and callable(abort):
            with contextlib.suppress(Exception):
                candidate = await detect(repo)
                if candidate is not None:
                    await abort(candidate)

    async def _destroy_container(
        self,
        attempt: AttemptModel | None,
        container: ManagedContainer | None = None,
    ) -> None:
        if self.sandbox is None:
            return
        if attempt is None and container is None:
            return
        container_id = (
            container.container_id
            if container is not None
            else (attempt.container_id if attempt is not None else None)
        ) or (f"ds-{attempt.id}" if attempt is not None else None)
        if container_id is None:
            return
        with contextlib.suppress(Exception):
            await self.sandbox.destroy(container_id)

    @staticmethod
    def _action(
        kind: RecoveryActionKind,
        task: TaskModel,
        attempt: AttemptModel | None,
        detail: str,
    ) -> RecoveryAction:
        return RecoveryAction(
            kind=kind,
            task_id=task.id,
            attempt_id=attempt.id if attempt is not None else None,
            detail=detail,
        )

    # --------------------------------------------------------------- orphans
    async def reap_orphans(self) -> list[str]:
        """Destroy managed containers that no active attempt owns."""
        if self.sandbox is None:
            return []
        async with self._session_factory() as session:
            active = {attempt.id: attempt for attempt in await AttemptRepo(session).list_active()}
        destroyed: list[str] = []
        for container in await self.sandbox.list_managed():
            attempt_id = container.labels.get("devsandbox.attempt")
            owned = attempt_id in active if attempt_id else False
            if owned:
                continue
            if container.container_id in {f"ds-{attempt_id}" for attempt_id in active}:
                continue
            with contextlib.suppress(Exception):
                await self.sandbox.destroy(container.container_id)
                destroyed.append(container.container_id)
        return destroyed

    async def _clean_orphan_workspaces(self) -> list[str]:
        """Remove workspaces older than the configured retention window."""
        lister = getattr(self.git, "list_workspaces", None)
        if not callable(lister):
            return []
        retention_seconds = max(0.0, self.config.workspace_retention_hours) * 3600.0
        now = self._clock().timestamp()
        removed: list[str] = []
        async with self._session_factory() as session:
            projects = await ProjectRepo(session, is_legal_project).list()
            active_paths = {
                attempt.workspace_path
                for attempt in await AttemptRepo(session).list_active()
                if attempt.workspace_path
            }
        for project in projects:
            with contextlib.suppress(Exception):
                for path in await lister(project.id):
                    resolved = str(path)
                    if resolved in active_paths:
                        continue
                    with contextlib.suppress(OSError):
                        if now - Path(path).stat().st_mtime < retention_seconds:
                            continue
                        shutil.rmtree(path, ignore_errors=True)
                        removed.append(resolved)
        return removed


def decisions_for_report(report: RecoveryReport) -> Iterable[RecoveryAction]:
    """Return actions that require the engine to resume work."""
    return (
        action
        for action in report.actions
        if action.kind
        in {
            RecoveryActionKind.RESUME_COLLECT,
            RecoveryActionKind.RERUN_VALIDATION,
            RecoveryActionKind.RESUME_VALIDATION_DECISION,
            RecoveryActionKind.RESUME_INTEGRATION,
            RecoveryActionKind.RESET_AND_RETRY,
        }
    )


__all__ = ["RecoveryService", "decisions_for_report"]
