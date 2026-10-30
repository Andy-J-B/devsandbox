"""Plan repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PlanModel


class PlanRepo:
    """Repository for plan records."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, plan_id: str) -> PlanModel | None:
        """Return the plan or ``None``."""
        result = await self.session.execute(select(PlanModel).where(PlanModel.id == plan_id))
        return result.scalar_one_or_none()

    async def get_active_for_project(self, project_id: str) -> PlanModel | None:
        """Return the active plan for a project, if any."""
        result = await self.session.execute(
            select(PlanModel).where(
                PlanModel.project_id == project_id, PlanModel.status == "active"
            )
        )
        return result.scalar_one_or_none()

    async def create(self, plan: PlanModel) -> PlanModel:
        """Persist a new plan."""
        self.session.add(plan)
        await self.session.flush()
        return plan
