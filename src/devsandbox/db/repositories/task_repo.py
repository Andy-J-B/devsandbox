"""Task repository with CAS-based status transitions and audit events."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...common.errors import InvalidTransition, TaskNotFoundError
from ...common.ids import TaskId
from ...common.time import utcnow
from ...common.types import TaskStatus
from ..models import EventModel, TaskDependencyModel, TaskModel

_TERMINAL_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
    TaskStatus.CANCELLED,
)


class TaskRepo:
    """Repository handling task lifecycle transitions."""

    def __init__(
        self,
        session: AsyncSession,
        is_legal: Callable[[TaskStatus, TaskStatus], bool] | None = None,
    ) -> None:
        self.session = session
        self._is_legal = is_legal

    def _legal(self, from_state: TaskStatus, to_state: TaskStatus) -> bool:
        if self._is_legal is None:
            return True
        return bool(self._is_legal(from_state, to_state))

    async def get(self, task_id: TaskId) -> TaskModel | None:
        """Return the task or ``None``."""
        result = await self.session.execute(select(TaskModel).where(TaskModel.id == task_id))
        return result.scalar_one_or_none()

    async def create(self, task: TaskModel) -> TaskModel:
        """Persist a new task."""
        self.session.add(task)
        await self.session.flush()
        return task

    async def transition(
        self,
        task_id: TaskId,
        from_states: list[TaskStatus],
        to_state: TaskStatus,
        reason: str,
        **fields: Any,
    ) -> TaskModel:
        """Atomically transition a task status (compare-and-swap) and audit the event.

        Raises :class:`InvalidTransition` when the current status is not among
        ``from_states``, when the transition is illegal, or when the CAS loses.
        """
        task = await self.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} not found")

        current = TaskStatus(task.status)
        if current not in from_states:
            raise InvalidTransition(
                f"task {task_id} is in state {current.value}, expected one of "
                f"{[s.value for s in from_states]}"
            )
        if not self._legal(current, to_state):
            raise InvalidTransition(
                f"illegal transition {current.value} -> {to_state.value} for task {task_id}"
            )

        values: dict[str, Any] = {
            "status": to_state.value,
            "version": task.version + 1,
            "updated_at": utcnow(),
        }
        values.update(fields)

        stmt = (
            update(TaskModel)
            .where(TaskModel.id == task_id, TaskModel.status == current.value)
            .values(**values)
            .returning(TaskModel.version)
        )
        result = await self.session.execute(stmt)
        version = result.scalar_one_or_none()
        if version is None:
            raise InvalidTransition(f"cas conflict transitioning task {task_id}")

        task.version = version
        task.status = to_state.value
        task.updated_at = utcnow()
        for key, value in fields.items():
            setattr(task, key, value)

        event = EventModel(
            project_id=task.project_id,
            task_id=task_id,
            attempt_id=task.current_attempt_id,
            type="task.transition",
            from_state=current.value,
            to_state=to_state.value,
            payload_json=json.dumps({"reason": reason, **fields}),
            at=utcnow(),
        )
        self.session.add(event)
        return task

    async def ready_candidates(self, project_id: str) -> list[TaskModel]:
        """Return pending tasks whose dependencies are all completed."""
        tasks = (
            (
                await self.session.execute(
                    select(TaskModel).where(TaskModel.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        if not tasks:
            return []
        completed: set[str] = set()
        for task in tasks:
            if task.status == TaskStatus.COMPLETED.value:
                completed.add(task.id)
        ready: list[TaskModel] = []
        for task in tasks:
            if task.status != TaskStatus.PENDING.value:
                continue
            deps = await self.session.execute(
                select(TaskDependencyModel.depends_on_task_id).where(
                    TaskDependencyModel.task_id == task.id
                )
            )
            dep_ids = list(deps.scalars().all())
            if all(dep in completed for dep in dep_ids):
                ready.append(task)
        return ready

    async def mark_dependents_blocked(self, task_id: str) -> list[TaskModel]:
        """Mark non-terminal dependents of ``task_id`` as BLOCKED."""
        stmt = (
            select(TaskModel)
            .join(TaskDependencyModel, TaskModel.id == TaskDependencyModel.task_id)
            .where(TaskDependencyModel.depends_on_task_id == task_id)
            .where(TaskModel.status.not_in([s.value for s in _TERMINAL_STATUSES]))
        )
        result = await self.session.execute(stmt)
        dependents = list(result.scalars().all())
        for task in dependents:
            task.status = TaskStatus.BLOCKED.value
            task.blocked_reason = "dependency failed"
            task.version = task.version + 1
            task.updated_at = utcnow()
        return dependents
