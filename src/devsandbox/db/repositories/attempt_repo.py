"""Attempt repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AttemptModel


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
