"""Real PostgreSQL backup/restore round-trip tests."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from devsandbox.db.backup import backup_database, restore_database

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "src" / "devsandbox" / "db" / "migrations"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
        reason="PostgreSQL client tools (pg_dump/pg_restore) are not installed",
    ),
]


async def _reset_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


async def _upgrade(database_url: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", database_url)
    await asyncio.to_thread(command.upgrade, config, "head")


async def _seed_project(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, name, mode, status, main_branch, staging_branch) "
                    "VALUES (:id, :name, :mode, :status, :main, :staging)"
                ),
                {
                    "id": "backup-project",
                    "name": "backup-demo",
                    "mode": "supervised",
                    "status": "loaded",
                    "main": "main",
                    "staging": "devsandbox/backup-project/staging",
                },
            )
    finally:
        await engine.dispose()


async def _project_name(database_url: str) -> str | None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT name FROM projects WHERE id = 'backup-project'")
            )
            return result.scalar_one_or_none()
    finally:
        await engine.dispose()


async def _tables(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        from sqlalchemy import inspect

        async with engine.connect() as connection:
            return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    finally:
        await engine.dispose()


async def test_backup_and_restore_round_trip(postgres_url: str, tmp_path: Path) -> None:
    await _reset_schema(postgres_url)
    await _upgrade(postgres_url)
    await _seed_project(postgres_url)

    destination = tmp_path / "devsandbox.dump"
    backup = await backup_database(postgres_url, destination)

    assert destination.is_file()
    assert destination.stat().st_size > 0
    assert Path(backup.manifest_path).is_file()
    assert backup.manifest.alembic_revision == "0007"
    assert backup.manifest.bytes == destination.stat().st_size

    # Simulate losing the database, then restore the dump into a clean schema.
    await _reset_schema(postgres_url)
    assert "projects" not in await _tables(postgres_url)

    restored = await restore_database(postgres_url, destination)

    assert restored.database
    assert restored.cleaned is True
    assert await _project_name(postgres_url) == "backup-demo"
    assert "alembic_version" in await _tables(postgres_url)


async def test_restore_refuses_tampered_dump(postgres_url: str, tmp_path: Path) -> None:
    await _reset_schema(postgres_url)
    await _upgrade(postgres_url)
    await _seed_project(postgres_url)

    destination = tmp_path / "devsandbox.dump"
    await backup_database(postgres_url, destination)
    with destination.open("ab") as handle:
        handle.write(b"tampered")

    from devsandbox.common.errors import ConfigError

    with pytest.raises(ConfigError, match="checksum"):
        await restore_database(postgres_url, destination)

    # The original row is still present because pg_restore never ran.
    assert await _project_name(postgres_url) == "backup-demo"
