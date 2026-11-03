"""Attempt repository."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AttemptModel

# Attempt statuses that still hold a reservation / need recovery.
ACTIVE_ATTEMPT_STATUSES: frozenset[str] = frozenset(
    {"scheduled", "running", "validating", "validated", "integrating", "recovering"}
)
TERMINAL_ATTEMPT_STATUSES: frozenset[str] = frozenset(
    {"completed", "succeeded", "failed", "cancelled", "released", "error", "abandoned"}
)


class AttemptRepo:
    """Repository for attempt records."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, attempt_id: str) -> AttemptModel | None:
        """Return the attempt or ``None``."""
        result = await self.session.execute(
            select(AttemptModel).where(AttemptModel.id == attempt_id)
        )
        return result.scalar_one_or_none()

    async def get_for_task(self, task_id: str, attempt_number: int) -> AttemptModel | None:
        """Return the attempt for a task with a given attempt number."""
        result = await self.session.execute(
            select(AttemptModel).where(
                AttemptModel.task_id == task_id, AttemptModel.number == attempt_number
            )
        )
        return result.scalar_one_or_none()

    async def create(self, attempt: AttemptModel) -> AttemptModel:
        """Persist a new attempt."""
        self.session.add(attempt)
        await self.session.flush()
        return attempt

    async def list_for_task(self, task_id: str) -> list[AttemptModel]:
        """Return attempts for a task ordered by attempt number."""
        result = await self.session.execute(
            select(AttemptModel)
            .where(AttemptModel.task_id == task_id)
            .order_by(AttemptModel.number, AttemptModel.created_at)
        )
        return list(result.scalars().all())

    async def latest_for_task(self, task_id: str) -> AttemptModel | None:
        """Return the newest attempt for a task, if any."""
        result = await self.session.execute(
            select(AttemptModel)
            .where(AttemptModel.task_id == task_id)
            .order_by(AttemptModel.number.desc(), AttemptModel.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def count_for_task(self, task_id: str) -> int:
        """Return how many attempt rows exist for a task."""
        result = await self.session.execute(
            select(func.count()).select_from(AttemptModel).where(AttemptModel.task_id == task_id)
        )
        return int(result.scalar_one())

    async def list_active(self) -> list[AttemptModel]:
        """Return attempts that are not terminal (recovery / reservations)."""
        result = await self.session.execute(
            select(AttemptModel)
            .where(AttemptModel.status.not_in(TERMINAL_ATTEMPT_STATUSES))
            .order_by(AttemptModel.created_at, AttemptModel.id)
        )
        return list(result.scalars().all())

    async def list_for_project(self, project_id: str) -> list[AttemptModel]:
        """Return attempts joined to their task's project."""
        from ..models import TaskModel

        result = await self.session.execute(
            select(AttemptModel)
            .join(TaskModel, TaskModel.id == AttemptModel.task_id)
            .where(TaskModel.project_id == project_id)
            .order_by(AttemptModel.created_at, AttemptModel.id)
        )
        return list(result.scalars().all())

    async def count_for_project(self, project_id: str) -> int:
        """Return the number of attempts created for a project."""
        from ..models import TaskModel

        result = await self.session.execute(
            select(func.count())
            .select_from(AttemptModel)
            .join(TaskModel, TaskModel.id == AttemptModel.task_id)
            .where(TaskModel.project_id == project_id)
        )
        return int(result.scalar_one())

    async def set_status(
        self,
        attempt_id: str,
        status: str,
        **fields: object,
    ) -> AttemptModel | None:
        """Update an attempt row; missing attempts are a no-op."""
        attempt = await self.get(attempt_id)
        if attempt is None:
            return None
        attempt.status = status
        for key, value in fields.items():
            setattr(attempt, key, value)
        await self.session.flush()
        return attempt
