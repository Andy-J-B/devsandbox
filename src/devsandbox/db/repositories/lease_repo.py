"""Lease repository for single-orchestrator coordination."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import LeaseModel


class LeaseRepo:
    """Repository for orchestrator lease guards."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, name: str) -> LeaseModel | None:
        """Return the lease or ``None``."""
        result = await self.session.execute(select(LeaseModel).where(LeaseModel.name == name))
        return result.scalar_one_or_none()

    async def acquire(self, name: str, holder: str, expires_at: datetime) -> LeaseModel:
        """Create or refresh a lease."""
        lease = LeaseModel(name=name, holder=holder, expires_at=expires_at)
        self.session.add(lease)
        await self.session.flush()
        return lease

    async def release(self, name: str) -> None:
        """Delete a lease."""
        lease = await self.get(name)
        if lease is not None:
            await self.session.delete(lease)
