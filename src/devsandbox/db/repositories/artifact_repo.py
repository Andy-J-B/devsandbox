"""Context artifact repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ContextArtifactModel


class ArtifactRepo:
    """Repository for versioned context artifacts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, artifact: ContextArtifactModel) -> ContextArtifactModel:
        """Persist a new context artifact."""
        self.session.add(artifact)
        await self.session.flush()
        return artifact

    async def list_for_project(self, project_id: str) -> list[ContextArtifactModel]:
        """Return artifacts for a project."""
        result = await self.session.execute(
            select(ContextArtifactModel).where(ContextArtifactModel.project_id == project_id)
        )
        return list(result.scalars().all())
