"""Uvicorn entrypoint and engine lifecycle for the HTTP API (plan 10)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI

from ..common.logging import get_logger
from ..config.settings import DevSandboxSettings, get_settings
from .app import create_app
from .bootstrap import build_container
from .container import AppContainer

_log = get_logger("devsandbox.api.server")


@asynccontextmanager
async def _engine_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the orchestration engine and audit shipper in the server loop.

    The engine is the only component that starts containers, so serving HTTP
    without it is useful only for inspection/debugging.  Audit shipping is
    independent of the engine and keeps running under ``--no-engine`` so the
    append-only event log still reaches the operator sink.  Shutdown is
    graceful: no new attempts are launched, then in-flight work is given the
    engine's configured timeout before cancellation.
    """
    container = app.state.container
    engine = container.engine if container is not None else None
    runner = getattr(engine, "run_forever", None)
    task: asyncio.Task[None] | None = None
    if runner is not None and _engine_enabled(container):
        task = asyncio.create_task(_run_engine(runner), name="devsandbox-engine")
    audit_task = _start_audit_shipper(container)
    try:
        yield
    finally:
        if audit_task is not None:
            audit_task.cancel()
            await asyncio.gather(audit_task, return_exceptions=True)
            shipper = getattr(container, "audit_shipper", None) if container is not None else None
            if shipper is not None:
                await shipper.aclose()
        if task is not None:
            request_shutdown = getattr(engine, "request_shutdown", None)
            if request_shutdown is not None:
                await request_shutdown()
            try:
                await asyncio.wait_for(task, timeout=60.0)
            except TimeoutError:
                _log.warning("engine did not stop within 60s; cancelling")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def _start_audit_shipper(container: AppContainer | None) -> asyncio.Task[None] | None:
    if container is None:
        return None
    shipper = getattr(container, "audit_shipper", None)
    if shipper is None:
        return None
    return asyncio.create_task(_run_audit_shipper(shipper), name="devsandbox-audit")


async def _run_audit_shipper(shipper: Any) -> None:
    """Run audit shipping, logging failures instead of losing the task."""
    try:
        await shipper.run_forever()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - rare startup/runtime failure
        _log.error(
            "audit shipper stopped unexpectedly",
            error_type=type(exc).__name__,
            error=str(exc),
        )


def _engine_enabled(container: AppContainer | None) -> bool:
    return bool(container is not None and container.settings.api.run_engine)


async def _run_engine(runner: Any) -> None:
    """Run the engine, logging failures instead of losing the task exception."""
    try:
        await runner()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - rare startup/runtime failure
        _log.error(
            "orchestration engine stopped unexpectedly",
            error_type=type(exc).__name__,
            error=str(exc),
        )


def create_server_app(container: AppContainer) -> FastAPI:
    """Create the app used by ``devsandbox serve`` (engine included)."""
    return create_app(container, lifespan=_engine_lifespan)


def run(
    settings: DevSandboxSettings | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    run_engine: bool | None = None,
    log_level: str = "info",
) -> None:
    """Build the container and serve the API until interrupted."""
    resolved = settings or get_settings()
    api = resolved.api.model_copy(
        update={
            "host": host if host is not None else resolved.api.host,
            "port": port if port is not None else resolved.api.port,
            "run_engine": (run_engine if run_engine is not None else resolved.api.run_engine),
        }
    )
    resolved = resolved.model_copy(update={"api": api})
    if resolved.migrations.auto_upgrade:
        # Run before the container builds its engine/session factory.  The
        # helper resolves the migrations from the installed package and takes
        # an advisory lock so overlapping container/systemd restarts cannot
        # race the schema.
        from ..db.migrations_runner import run_startup_migrations

        _log.info(
            "applying database migrations",
            lock_timeout_seconds=resolved.migrations.lock_timeout_seconds,
        )
        run_startup_migrations(
            resolved.database_url,
            lock_timeout_seconds=resolved.migrations.lock_timeout_seconds,
        )
    container = build_container(resolved)
    app = create_server_app(container)
    _log.info(
        "starting DevSandbox API",
        host=resolved.api.host,
        port=resolved.api.port,
        engine=resolved.api.run_engine,
    )
    uvicorn.run(
        app,
        host=resolved.api.host,
        port=resolved.api.port,
        log_level=log_level,
        access_log=False,
    )


def main() -> None:
    """``python -m devsandbox.api`` / ``devsandbox-api`` entrypoint."""
    run()


__all__ = ["create_server_app", "main", "run"]
