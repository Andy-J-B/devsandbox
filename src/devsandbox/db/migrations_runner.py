"""Packaged Alembic migrations and a startup-safe upgrade helper.

The orchestrator can be started from a checkout, a wheel, a container, or a
systemd unit.  In every case it must be able to apply the migrations that ship
inside the installed package, without assuming a repository root or an
``alembic.ini`` on disk.

``upgrade_database`` serializes concurrent upgrades with a PostgreSQL advisory
lock.  That matters when a service manager restarts several workers at once or
when a deployment replaces a container before the old one has fully exited:
only one process runs the migration chain, and the others wait or fail with a
clear timeout instead of racing the schema.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ..common.errors import DevSandboxError
from ..common.logging import get_logger

_log = get_logger("devsandbox.db.migrations")

# 64-bit constant used by pg_advisory_lock.  It names the migration stream, not
# a particular database, which is safe because advisory locks are per-database.
MIGRATION_LOCK_KEY = 0x4453564D49475231
_LOCK_POLL_SECONDS = 0.25


class MigrationError(DevSandboxError):
    """Raised when startup migrations cannot be applied."""


class MigrationLockTimeoutError(MigrationError):
    """Raised when another process holds the migration lock for too long."""


def alembic_config(database_url: str) -> Config:
    """Return an Alembic config that resolves the packaged migrations.

    ``script_location`` points at this package (``site-packages/devsandbox/db``
    in an installed wheel and ``src/devsandbox/db`` in a checkout), so callers
    never depend on the current working directory.
    """
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _upgrade_to_head(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")


async def _read_revision(engine: AsyncEngine) -> str | None:
    """Return the current Alembic revision, or ``None`` for an empty schema."""
    async with engine.connect() as connection:
        try:
            if engine.dialect.name == "postgresql":
                present = await connection.execute(
                    text("SELECT to_regclass('public.alembic_version')")
                )
                if present.scalar_one_or_none() is None:
                    return None
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
        except Exception:
            return None
        return result.scalar_one_or_none()


async def _acquire_lock(connection: AsyncConnection, timeout_seconds: float) -> None:
    """Acquire the session-level advisory lock or raise after ``timeout_seconds``."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        result = await connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY}
        )
        if bool(result.scalar_one()):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MigrationLockTimeoutError(
                "timed out waiting for the database migration lock; another "
                "DevSandbox process is still migrating the schema"
            )
        await asyncio.sleep(min(_LOCK_POLL_SECONDS, remaining))


async def _release_lock(connection: AsyncConnection) -> None:
    with contextlib.suppress(Exception):
        await connection.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
        )


async def upgrade_database(
    database_url: str,
    *,
    lock_timeout_seconds: float = 120.0,
) -> str | None:
    """Apply every packaged migration and return the resulting revision.

    PostgreSQL deployments take an advisory lock for the whole upgrade.  Other
    dialects (used only by focused tests) run the chain directly.  The helper is
    idempotent: an already-current schema is a cheap read plus no-op.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        if engine.dialect.name == "postgresql":
            async with engine.connect() as connection:
                await _acquire_lock(connection, lock_timeout_seconds)
                try:
                    await asyncio.to_thread(_upgrade_to_head, database_url)
                finally:
                    await _release_lock(connection)
        else:
            await asyncio.to_thread(_upgrade_to_head, database_url)
        revision = await _read_revision(engine)
        _log.info("database migrations are current", revision=revision or "unknown")
        return revision
    except MigrationError:
        raise
    except Exception as exc:  # pragma: no cover - depends on the database driver
        raise MigrationError(f"database migration failed: {exc}") from exc
    finally:
        await engine.dispose()


def run_startup_migrations(
    database_url: str,
    *,
    lock_timeout_seconds: float = 120.0,
) -> str | None:
    """Synchronous wrapper used by ``serve`` and ``devsandbox init-db``."""
    return asyncio.run(upgrade_database(database_url, lock_timeout_seconds=lock_timeout_seconds))


__all__ = [
    "MIGRATION_LOCK_KEY",
    "MigrationError",
    "MigrationLockTimeoutError",
    "alembic_config",
    "run_startup_migrations",
    "upgrade_database",
]
