"""Task repository with CAS-based status transitions and audit events."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...common.errors import InvalidTransition, TaskNotFoundError
from ...common.ids import TaskId
from ...common.time import utcnow
from ...common.types import TaskStatus
from ..models import EventModel, TaskDependencyModel, TaskModel
from ._json import jsonable


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

    async def list_for_plan(self, plan_id: str) -> list[TaskModel]:
        """Return all tasks in a plan ordered by insertion id."""
        result = await self.session.execute(
            select(TaskModel).where(TaskModel.plan_id == plan_id).order_by(TaskModel.id)
        )
        return list(result.scalars().all())

    async def list_for_project(
        self,
        project_id: str,
        *,
        statuses: list[TaskStatus] | None = None,
        plan_id: str | None = None,
    ) -> list[TaskModel]:
        """Return tasks for a project, optionally filtered by status/plan."""
        stmt = select(TaskModel).where(TaskModel.project_id == project_id)
        if statuses is not None:
            stmt = stmt.where(TaskModel.status.in_([status.value for status in statuses]))
        if plan_id is not None:
            stmt = stmt.where(TaskModel.plan_id == plan_id)
        stmt = stmt.order_by(TaskModel.created_at, TaskModel.id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_status(self, statuses: list[TaskStatus]) -> list[TaskModel]:
        """Return all tasks in any of ``statuses`` (across projects)."""
        result = await self.session.execute(
            select(TaskModel)
            .where(TaskModel.status.in_([status.value for status in statuses]))
            .order_by(TaskModel.created_at, TaskModel.id)
        )
        return list(result.scalars().all())

    async def count_for_project(self, project_id: str) -> int:
        """Return the number of task rows for a project."""
        result = await self.session.execute(
            select(func.count()).select_from(TaskModel).where(TaskModel.project_id == project_id)
        )
        return int(result.scalar_one())

    async def get_by_key(self, plan_id: str, key: str) -> TaskModel | None:
        """Return the task with ``key`` inside a plan."""
        result = await self.session.execute(
            select(TaskModel).where(TaskModel.plan_id == plan_id, TaskModel.key == key)
        )
        return result.scalar_one_or_none()

    async def dependencies_for_task(self, task_id: str) -> list[str]:
        """Return dependency task ids for one task."""
        result = await self.session.execute(
            select(TaskDependencyModel.depends_on_task_id).where(
                TaskDependencyModel.task_id == task_id
            )
        )
        return list(result.scalars().all())

    async def add_dependency(self, task_id: str, depends_on_task_id: str) -> None:
        """Create a dependency edge."""
        self.session.add(
            TaskDependencyModel(task_id=task_id, depends_on_task_id=depends_on_task_id)
        )
        await self.session.flush()

    async def delete_for_plan(self, plan_id: str) -> None:
        """Delete all tasks and dependency edges belonging to a plan."""
        task_ids = select(TaskModel.id).where(TaskModel.plan_id == plan_id)
        await self.session.execute(
            delete(TaskDependencyModel).where(TaskDependencyModel.task_id.in_(task_ids))
        )
        await self.session.execute(
            delete(TaskDependencyModel).where(
                TaskDependencyModel.depends_on_task_id.in_(
                    select(TaskModel.id).where(TaskModel.plan_id == plan_id)
                )
            )
        )
        await self.session.execute(delete(TaskModel).where(TaskModel.plan_id == plan_id))
        await self.session.flush()

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
            payload_json=jsonable({"reason": reason, **fields}),
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

    async def mark_dependents_blocked(
        self, task_id: str, *, reason: str = "dependency failed"
    ) -> list[TaskModel]:
        """Mark transitive dependents of ``task_id`` as BLOCKED.

        Every status change goes through :meth:`transition` so the CAS and
        audit guarantees hold for blocked propagation too (00 §7).
        """
        blocked: list[TaskModel] = []
        frontier = [task_id]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            stmt = (
                select(TaskModel)
                .join(TaskDependencyModel, TaskModel.id == TaskDependencyModel.task_id)
                .where(TaskDependencyModel.depends_on_task_id == current)
                .where(TaskModel.status.in_([TaskStatus.PENDING.value, TaskStatus.READY.value]))
            )
            dependents = list((await self.session.execute(stmt)).scalars().all())
            for task in dependents:
                try:
                    updated = await self.transition(
                        TaskId(task.id),
                        [TaskStatus.PENDING, TaskStatus.READY],
                        TaskStatus.BLOCKED,
                        reason,
                        blocked_reason=reason,
                    )
                except InvalidTransition:
                    continue
                blocked.append(updated)
                frontier.append(task.id)
        return blocked
