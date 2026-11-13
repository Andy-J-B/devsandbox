"""Drill: the engine reconnects after PostgreSQL restarts.

Runs against a dedicated testcontainers PostgreSQL instance so the developer's
local database is never stopped.  The engine loop must pause, retry transient
disconnects, and resume without losing its lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.e2e.agents import ScriptedAgent
from tests.e2e.harness import DEFAULT_IMAGE, build_context, make_settings

from devsandbox.db.models import Base
from devsandbox.orchestrator.models import OrchestratorConfig
from devsandbox.sandbox.manager import DockerSandboxManager

pytestmark = [pytest.mark.drill, pytest.mark.docker, pytest.mark.slow]


def _free_port() -> int:
    """Reserve a fixed loopback port so restart does not remap it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _postgres_container(image: str) -> Any:
    """Start a dedicated restartable PostgreSQL container."""
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - older testcontainers layout
        from testcontainers.postgresql import PostgresContainer
    container = PostgresContainer(image, driver="asyncpg")
    # Testcontainers normally uses an ephemeral host port, which Docker
    # Desktop can reassign after a restart.  A fixed port keeps the connection
    # URL valid across the drill.
    container.with_bind_ports(5432, _free_port())
    container.start()
    return container


@pytest.fixture
def restartable_postgres(require_docker: None) -> Any:
    """Provide a dedicated PostgreSQL container that the drill may restart."""
    image = os.environ.get("DEVSANDBOX_DRILL_POSTGRES_IMAGE", "postgres:16")
    try:
        container = _postgres_container(image)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"could not start a drill PostgreSQL container: {exc}")
    try:
        yield container
    finally:
        with contextlib.suppress(Exception):
            container.stop()


async def _wait_for(predicate: Callable[[], bool], *, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition was not met within {timeout:.1f}s")


async def _wait_for_database(engine: Any, *, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("select 1"))
            return
        except Exception:
            await asyncio.sleep(0.2)
    raise AssertionError(f"PostgreSQL did not become reachable within {timeout:.1f}s")


async def test_engine_survives_a_postgresql_restart(
    restartable_postgres: Any,
    tmp_path: Path,
) -> None:
    url = restartable_postgres.get_connection_url()
    db_engine = create_async_engine(url, pool_pre_ping=True)
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    try:
        async with db_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        settings = make_settings(data_root=data_root, database_url=url)
        sandbox = DockerSandboxManager(settings, data_root)
        agent = ScriptedAgent(sandbox, settings, image=DEFAULT_IMAGE)
        context = build_context(
            settings=settings,
            session_factory=maker,
            agent=agent,
            data_root=data_root,
            sandbox=sandbox,
            orchestrator_config=OrchestratorConfig(
                lease_ttl_seconds=60,
                tick_seconds=0.05,
                idle_sleep_seconds=0.01,
                db_reconnect_base_seconds=0.1,
                db_reconnect_max_seconds=0.5,
                shutdown_timeout_seconds=5.0,
            ),
        )
        engine = context.engine

        engine_task = asyncio.create_task(engine.run_forever())
        try:
            await _wait_for(lambda: engine._lease_acquired, timeout=20.0)

            wrapped = restartable_postgres.get_wrapped_container()
            await asyncio.to_thread(wrapped.stop, timeout=2)
            await asyncio.sleep(1.0)
            await asyncio.to_thread(wrapped.start)
            await _wait_for_database(db_engine, timeout=30.0)
            # Give the loop time to complete a tick against the restarted server.
            await asyncio.sleep(1.0)

            assert not engine_task.done(), "engine exited instead of reconnecting"
            assert engine._lease_acquired, "engine lost its lease across the restart"
            async with maker() as session:
                await session.execute(text("select 1"))
        finally:
            await engine.request_shutdown()
            await asyncio.wait_for(engine_task, timeout=20.0)
    finally:
        await db_engine.dispose()
