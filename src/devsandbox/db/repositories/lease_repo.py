"""Lease repository for single-orchestrator coordination."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...common.time import utcnow
from ..models import LeaseModel


class LeaseRepo:
    """Repository for orchestrator lease guards.

    The lease row is the only cross-process lock: acquiring it is a
    ``SELECT ... FOR UPDATE`` followed by an insert/update, so exactly one
    orchestrator can own the lease at a time.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, name: str) -> LeaseModel | None:
        """Return the lease or ``None``."""
        result = await self.session.execute(select(LeaseModel).where(LeaseModel.name == name))
        return result.scalar_one_or_none()

    async def try_acquire(self, name: str, holder: str, ttl_seconds: int = 60) -> LeaseModel | None:
        """Acquire or renew the lease; return ``None`` if another holder owns it."""
        now = utcnow()
        expires_at = now + timedelta(seconds=ttl_seconds)
        lease = (
            await self.session.execute(
                select(LeaseModel).where(LeaseModel.name == name).with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None:
            lease = LeaseModel(name=name, holder=holder, expires_at=expires_at)
            self.session.add(lease)
            try:
                await self.session.flush()
            except IntegrityError:
                # A concurrent acquire won the insert race.
                await self.session.rollback()
                return None
            return lease
        if lease.holder != holder and lease.expires_at > now:
            return None
        lease.holder = holder
        lease.expires_at = expires_at
        await self.session.flush()
        return lease

    async def refresh(self, name: str, holder: str, ttl_seconds: int = 60) -> bool:
        """Extend a lease owned by ``holder``; return ``False`` if lost."""
        lease = (
            await self.session.execute(
                select(LeaseModel).where(LeaseModel.name == name).with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None or lease.holder != holder:
            return False
        lease.expires_at = utcnow() + timedelta(seconds=ttl_seconds)
        await self.session.flush()
        return True

    async def acquire(self, name: str, holder: str, expires_at: datetime) -> LeaseModel:
        """Create or replace a lease (legacy helper, no ownership check)."""
        lease = await self.get(name)
        if lease is None:
            lease = LeaseModel(name=name, holder=holder, expires_at=expires_at)
            self.session.add(lease)
        else:
            lease.holder = holder
            lease.expires_at = expires_at
        await self.session.flush()
        return lease

    async def release(self, name: str, holder: str | None = None) -> None:
        """Release the lease (only when ``holder`` matches, if provided)."""
        lease = await self.get(name)
        if lease is None:
            return
        if holder is not None and lease.holder != holder:
            return
        await self.session.delete(lease)
        await self.session.flush()
