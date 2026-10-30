"""Cost / usage ledger repository."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import CostEntryModel, UsageCounterModel


class CostRepo:
    """Repository for cost ledger and usage counters."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_cost(self, entry: CostEntryModel) -> CostEntryModel:
        """Append a cost ledger entry."""
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def bump_usage(self, counter: UsageCounterModel) -> UsageCounterModel:
        """Create or update a usage counter."""
        self.session.add(counter)
        await self.session.flush()
        return counter
