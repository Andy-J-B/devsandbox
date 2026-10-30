"""Integration repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import IntegrationModel


class IntegrationRepo:
    """Repository for staging integration records."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, integration: IntegrationModel) -> IntegrationModel:
        """Persist a new integration record."""
        self.session.add(integration)
        await self.session.flush()
        return integration

    async def list_for_task(self, task_id: str) -> list[IntegrationModel]:
        """Return integration records for a task."""
        result = await self.session.execute(
            select(IntegrationModel).where(IntegrationModel.task_id == task_id)
        )
        return list(result.scalars().all())
