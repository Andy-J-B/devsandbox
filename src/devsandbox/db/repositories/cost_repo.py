"""Cost / usage ledger repository."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
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

    async def total_cost(self, project_id: str, kind: str | None = None) -> float:
        """Return total cost for a project, optionally by ledger kind."""
        stmt = select(func.coalesce(func.sum(CostEntryModel.amount_usd), 0)).where(
            CostEntryModel.project_id == project_id
        )
        if kind is not None:
            stmt = stmt.where(CostEntryModel.kind == kind)
        return float((await self.session.execute(stmt)).scalar_one())

    async def usage_totals(self, project_id: str) -> tuple[int, int]:
        """Return ``(tokens, requests)`` recorded for a project."""
        result = await self.session.execute(
            select(
                func.coalesce(func.sum(UsageCounterModel.tokens), 0),
                func.coalesce(func.sum(UsageCounterModel.requests), 0),
            ).where(UsageCounterModel.project_id == project_id)
        )
        tokens, requests = result.one()
        return int(tokens), int(requests)

    async def usage_in_window(
        self,
        project_id: str,
        provider: str,
        window_start: datetime,
    ) -> tuple[int, int]:
        """Return ``(tokens, requests)`` for one provider/window pair."""
        result = await self.session.execute(
            select(
                func.coalesce(func.sum(UsageCounterModel.tokens), 0),
                func.coalesce(func.sum(UsageCounterModel.requests), 0),
            ).where(
                UsageCounterModel.project_id == project_id,
                UsageCounterModel.provider == provider,
                UsageCounterModel.window_start == window_start,
            )
        )
        tokens, requests = result.one()
        return int(tokens), int(requests)

    async def inference_usage_totals(
        self,
        project_id: str,
        *,
        note_prefix: str | None = None,
    ) -> tuple[int, int]:
        """Return ``(tokens, requests)`` derived from inference cost entries.

        This is the reconciliation side of the ledger: each inference entry
        carries its ``TokenUsage`` in ``units_json`` and represents one model
        request.  ``note_prefix`` restricts the check to one producer (for
        example ``agent:``).
        """
        stmt = select(CostEntryModel).where(
            CostEntryModel.project_id == project_id,
            CostEntryModel.kind == "inference",
        )
        if note_prefix is not None:
            stmt = stmt.where(CostEntryModel.note.startswith(note_prefix))
        entries = list((await self.session.execute(stmt)).scalars().all())
        tokens = 0
        for entry in entries:
            units = entry.units_json or {}
            raw = units.get("total_tokens", 0) if isinstance(units, dict) else 0
            try:
                tokens += int(raw or 0)
            except (TypeError, ValueError):
                continue
        return tokens, len(entries)

    async def record_usage(
        self,
        *,
        project_id: str,
        provider: str,
        window_start: datetime,
        tokens: int = 0,
        requests: int = 0,
    ) -> UsageCounterModel:
        """Increment a usage counter for a provider window (upsert)."""
        result = await self.session.execute(
            select(UsageCounterModel).where(
                UsageCounterModel.project_id == project_id,
                UsageCounterModel.provider == provider,
                UsageCounterModel.window_start == window_start,
            )
        )
        counter = result.scalar_one_or_none()
        if counter is None:
            counter = UsageCounterModel(
                project_id=project_id,
                provider=provider,
                window_start=window_start,
                tokens=tokens,
                requests=requests,
            )
            self.session.add(counter)
        else:
            counter.tokens += tokens
            counter.requests += requests
        await self.session.flush()
        return counter
