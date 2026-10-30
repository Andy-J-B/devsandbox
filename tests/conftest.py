"""Shared pytest fixtures for DevSandbox tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest


@pytest.fixture
def frozen_clock():
    """Replace the DevSandbox clock with a controllable fake."""
    from devsandbox.common.time import set_utcnow

    class FakeClock:
        def __init__(self) -> None:
            self.now_value = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        def now(self) -> datetime:
            return self.now_value

    fake = FakeClock()
    set_utcnow(fake.now)
    yield fake
    set_utcnow(lambda: datetime.now(UTC))


@pytest.fixture
def redactor():
    """Return a fresh redactor instance."""
    from devsandbox.common.redaction import Redactor

    return Redactor()


@pytest.fixture
def settings():
    """Return process-wide DevSandbox settings."""
    from devsandbox.config import settings as settings_module

    settings_module.reset_settings_cache()
    return settings_module.get_settings()


@pytest.fixture(scope="session")
def postgres_container():
    """Start an ephemeral Postgres container, or ``None`` when unavailable."""
    try:
        from testcontainers.postgresql import PostgresContainer

        container = PostgresContainer("postgres:16")
        container.start()
        return container
    except Exception:
        return None


@pytest.fixture(scope="session")
def postgres_url(postgres_container) -> str:
    """Return the ephemeral Postgres connection URL."""
    if postgres_container is None:
        pytest.skip("testcontainers-postgres is unavailable")
    return postgres_container.get_connection_url()


@pytest.fixture
async def db_engine(postgres_url):
    """Create an async engine bound to the ephemeral Postgres."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from devsandbox.db.models import Base

    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    """Provide a session inside a single transaction."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        yield session
