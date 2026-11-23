"""DB-backed model usage store used by the adapter and orchestrator.

``UsageLimiter`` deliberately knows nothing about SQLAlchemy.  This module is
the production bridge: it reads and writes ``usage_counters`` through a
short-lived session so budget checks at the adapter boundary see exactly the
same numbers as the scheduler and the API.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.repositories.cost_repo import CostRepo
from .models import UsageSnapshot


def parse_window(window: str) -> datetime:
    """Parse a usage window key into a timezone-aware timestamp.

    ``UsageLimiter`` stores windows as ISO-8601 strings.  A date-only value is
    accepted for backwards compatibility with the original daily window.
    """
    value = datetime.fromisoformat(window)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SqlUsageStore:
    """Read/write model usage counters through an async session factory."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(self, project_id: str, provider: str, window: str) -> UsageSnapshot:
        """Return current usage for ``project_id``/provider/window."""
        start = parse_window(window)
        async with self._session_factory() as session:
            tokens, requests = await CostRepo(session).usage_in_window(project_id, provider, start)
        return UsageSnapshot(tokens=tokens, requests=requests)

    async def record(
        self,
        project_id: str,
        provider: str,
        window: str,
        tokens: int,
        requests: int,
    ) -> None:
        """Increment the usage counter, tolerating concurrent inserts."""
        start = parse_window(window)
        async with self._session_factory() as session:
            try:
                await CostRepo(session).record_usage(
                    project_id=project_id,
                    provider=provider,
                    window_start=start,
                    tokens=tokens,
                    requests=requests,
                )
                await session.commit()
            except IntegrityError:
                # A concurrent attempt inserted the same counter first.  The
                # rollback discards our failed insert; re-run the read/update
                # path now that the row exists.
                await session.rollback()
                await CostRepo(session).record_usage(
                    project_id=project_id,
                    provider=provider,
                    window_start=start,
                    tokens=tokens,
                    requests=requests,
                )
                await session.commit()


__all__ = ["SqlUsageStore", "parse_window"]
