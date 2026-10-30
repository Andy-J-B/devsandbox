"""Project repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ProjectModel


class ProjectRepo:
    """Repository for project records."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, project_id: str) -> ProjectModel | None:
        """Return the project or ``None``."""
        result = await self.session.execute(
            select(ProjectModel).where(ProjectModel.id == project_id)
        )
        return result.scalar_one_or_none()

    async def create(self, project: ProjectModel) -> ProjectModel:
        """Persist a new project."""
        self.session.add(project)
        await self.session.flush()
        return project

    async def list(self) -> list[ProjectModel]:
        """Return all projects."""
        result = await self.session.execute(select(ProjectModel))
        return list(result.scalars().all())
