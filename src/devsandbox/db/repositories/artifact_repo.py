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

    async def get(self, artifact_id: str) -> ContextArtifactModel | None:
        """Return one artifact by id."""
        result = await self.session.execute(
            select(ContextArtifactModel).where(ContextArtifactModel.id == artifact_id)
        )
        return result.scalar_one_or_none()

    async def list_for_project(
        self, project_id: str, kind: str | None = None
    ) -> list[ContextArtifactModel]:
        """Return artifacts for a project, newest first."""
        statement = select(ContextArtifactModel).where(
            ContextArtifactModel.project_id == project_id
        )
        if kind is not None:
            statement = statement.where(ContextArtifactModel.kind == kind)
        statement = statement.order_by(
            ContextArtifactModel.created_at.desc(), ContextArtifactModel.id.desc()
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def latest_for_project(self, project_id: str, kind: str) -> ContextArtifactModel | None:
        """Return the newest artifact of ``kind`` for a project."""
        artifacts = await self.list_for_project(project_id, kind)
        return artifacts[0] if artifacts else None

    async def mark_stale(self, artifact_id: str, stale: bool = True) -> None:
        """Mark an artifact stale without deleting its history."""
        artifact = await self.get(artifact_id)
        if artifact is not None:
            artifact.stale = stale
            await self.session.flush()
