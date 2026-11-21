"""Alembic upgrade/downgrade round-trip tests (PostgreSQL)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "src" / "devsandbox" / "db" / "migrations"

APP_TABLES = {
    "projects",
    "plans",
    "tasks",
    "task_dependencies",
    "attempts",
    "events",
    "integrations",
    "context_artifacts",
    "cost_entries",
    "usage_counters",
    "leases",
}


def _config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


async def _run_alembic(database_url: str, action: str, revision: str) -> None:
    config = _config(database_url)
    if action == "upgrade":
        await asyncio.to_thread(command.upgrade, config, revision)
    elif action == "downgrade":
        await asyncio.to_thread(command.downgrade, config, revision)
    else:  # pragma: no cover - test helper guard
        raise ValueError(f"unknown alembic action {action!r}")


async def _reset_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


async def _tables(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    finally:
        await engine.dispose()


async def _columns(database_url: str, table: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.run_sync(lambda sync: inspect(sync).get_columns(table))
        return {row["name"] for row in rows}
    finally:
        await engine.dispose()


async def _revision(database_url: str) -> str | None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("select version_num from alembic_version"))
            return result.scalar_one_or_none()
    finally:
        await engine.dispose()


async def test_migrations_upgrade_downgrade_and_upgrade_again(postgres_url: str) -> None:
    await _reset_schema(postgres_url)

    await _run_alembic(postgres_url, "upgrade", "head")
    assert APP_TABLES.issubset(await _tables(postgres_url))
    assert await _revision(postgres_url) == "0007"

    await _run_alembic(postgres_url, "downgrade", "base")
    remaining = await _tables(postgres_url)
    assert APP_TABLES.isdisjoint(remaining)

    await _run_alembic(postgres_url, "upgrade", "head")
    assert APP_TABLES.issubset(await _tables(postgres_url))
    assert await _revision(postgres_url) == "0007"


async def test_migration_0007_downgrade_removes_paused_then_upgrade_restores(
    postgres_url: str,
) -> None:
    await _reset_schema(postgres_url)
    await _run_alembic(postgres_url, "upgrade", "head")

    assert "paused" in await _columns(postgres_url, "tasks")
    assert "allow_no_changes" in await _columns(postgres_url, "tasks")

    await _run_alembic(postgres_url, "downgrade", "0006")

    assert await _revision(postgres_url) == "0006"
    assert "paused" not in await _columns(postgres_url, "tasks")
    assert "allow_no_changes" in await _columns(postgres_url, "tasks")

    await _run_alembic(postgres_url, "upgrade", "head")

    assert await _revision(postgres_url) == "0007"
    assert "paused" in await _columns(postgres_url, "tasks")


async def test_downgrade_without_version_table_is_a_noop(postgres_url: str) -> None:
    """Running downgrade on a fresh schema must not create or destroy anything."""
    await _reset_schema(postgres_url)

    await _run_alembic(postgres_url, "downgrade", "base")

    assert APP_TABLES.isdisjoint(await _tables(postgres_url))
