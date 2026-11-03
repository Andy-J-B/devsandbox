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
            select(IntegrationModel)
            .where(IntegrationModel.task_id == task_id)
            .order_by(IntegrationModel.created_at)
        )
        return list(result.scalars().all())

    async def get(self, integration_id: str) -> IntegrationModel | None:
        """Return one integration record."""
        result = await self.session.execute(
            select(IntegrationModel).where(IntegrationModel.id == integration_id)
        )
        return result.scalar_one_or_none()

    async def latest_for_attempt(self, attempt_id: str) -> IntegrationModel | None:
        """Return the newest integration record for an attempt."""
        result = await self.session.execute(
            select(IntegrationModel)
            .where(IntegrationModel.attempt_id == attempt_id)
            .order_by(IntegrationModel.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def latest_for_task(self, task_id: str) -> IntegrationModel | None:
        """Return the newest integration record for a task."""
        result = await self.session.execute(
            select(IntegrationModel)
            .where(IntegrationModel.task_id == task_id)
            .order_by(IntegrationModel.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_for_project(self, project_id: str) -> list[IntegrationModel]:
        """Return integration records for a project."""
        result = await self.session.execute(
            select(IntegrationModel)
            .where(IntegrationModel.project_id == project_id)
            .order_by(IntegrationModel.created_at)
        )
        return list(result.scalars().all())
