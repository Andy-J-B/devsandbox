"""Event repository (append-only audit log)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import EventModel


class EventRepo:
    """Repository for audit events."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, event: EventModel) -> EventModel:
        """Append an event."""
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_for_project(self, project_id: str, limit: int = 100) -> list[EventModel]:
        """Return recent events for a project."""
        result = await self.session.execute(
            select(EventModel)
            .where(EventModel.project_id == project_id)
            .order_by(EventModel.at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
