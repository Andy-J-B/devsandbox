"""Async SQLAlchemy engine/session wiring."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config.settings import get_settings

engine: AsyncEngine = create_async_engine(
    get_settings().database_url,
    echo=False,
    pool_pre_ping=True,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def unit_of_work() -> AsyncIterator[AsyncSession]:
    """Yield a session spanning one committed transaction."""
    async with AsyncSessionLocal() as session, session.begin():
        yield session


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session (caller manages transaction boundaries)."""
    async with AsyncSessionLocal() as session:
        yield session
