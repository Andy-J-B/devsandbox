"""Shared pytest fixtures for DevSandbox tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply the suite's directory-based markers.

    Keeping the marker mapping here (instead of repeating it on every test
    module) makes ``-m unit``, ``-m e2e`` and friends reliable even for older
    tests that predate the marker taxonomy.
    """
    root = Path(__file__).resolve().parent
    directories = {
        root / "unit": "unit",
        root / "integration": "integration",
        root / "e2e": "e2e",
        root / "security": "security",
        root / "contract": "contract",
        root / "drills": "drill",
    }
    for item in items:
        path = Path(str(item.path)).resolve()
        for directory, marker in directories.items():
            if path.is_relative_to(directory):
                item.add_marker(getattr(pytest.mark, marker))
                break
        else:
            item.add_marker(pytest.mark.unit)


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
    """Start an ephemeral Postgres container, or ``None`` when unavailable.

    Setting ``DEVSANDBOX_TEST_DATABASE_URL`` opts out of testcontainers so CI
    can point the suite at a PostgreSQL service instead of starting a
    duplicate container on the same Docker daemon.
    """
    if os.environ.get("DEVSANDBOX_TEST_DATABASE_URL"):
        return None
    try:
        try:
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:  # pragma: no cover - older testcontainers layout
            from testcontainers.postgresql import PostgresContainer

        container = PostgresContainer("postgres:16", driver="asyncpg")
        container.start()
        return container
    except Exception:
        return None


@pytest.fixture(scope="session")
def postgres_url(postgres_container) -> str:
    """Return the Postgres connection URL used by integration tests.

    Prefers an explicit ``DEVSANDBOX_TEST_DATABASE_URL`` (for CI services),
    then a throwaway testcontainers instance, and finally the configured
    ``DEVSANDBOX_DATABASE_URL`` (local dev database) when Docker is unavailable.
    """
    explicit = os.environ.get("DEVSANDBOX_TEST_DATABASE_URL")
    if explicit:
        return explicit
    if postgres_container is not None:
        return postgres_container.get_connection_url()
    from devsandbox.config import settings as settings_module

    return settings_module.get_settings().database_url


@pytest.fixture
async def db_engine(postgres_url):
    """Create an async engine bound to the ephemeral Postgres."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from devsandbox.db.models import Base

    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    """Provide a session inside an autobegun transaction that is always rolled back."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with maker() as session:
        try:
            yield session
        finally:
            await session.rollback()
