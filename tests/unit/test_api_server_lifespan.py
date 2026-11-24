"""Server lifespan: start the engine, then stop it gracefully on shutdown."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from tests.api_helpers import make_settings

from devsandbox.api.container import AppContainer
from devsandbox.api.server import create_server_app


class _LifespanEngine:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.shutdown_calls = 0

    async def run_forever(self) -> None:
        self.started.set()
        await self.stopped.wait()

    async def request_shutdown(self) -> None:
        self.shutdown_calls += 1
        self.stopped.set()


class _CrashingEngine:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.shutdown_calls = 0

    async def run_forever(self) -> None:
        self.started.set()
        raise RuntimeError("engine failed on startup")

    async def request_shutdown(self) -> None:
        self.shutdown_calls += 1


def _container(settings: Any, engine: Any) -> AppContainer:
    return cast(
        AppContainer,
        SimpleNamespace(settings=settings, version="test", engine=engine),
    )


def _settings_for_engine(tmp_path) -> Any:
    settings = make_settings(str(tmp_path / "data"))
    return settings.model_copy(update={"api": settings.api.model_copy(update={"run_engine": True})})


async def test_lifespan_starts_and_stops_the_engine(tmp_path) -> None:
    settings = _settings_for_engine(tmp_path)
    engine = _LifespanEngine()
    app = create_server_app(_container(settings, engine))

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(engine.started.wait(), timeout=2)
        assert engine.shutdown_calls == 0

    assert engine.shutdown_calls == 1


async def test_lifespan_skips_the_engine_when_disabled(tmp_path) -> None:
    settings = make_settings(str(tmp_path / "data"))
    engine = _LifespanEngine()
    app = create_server_app(_container(settings, engine))

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.01)

    assert engine.started.is_set() is False
    assert engine.shutdown_calls == 0


async def test_engine_crash_does_not_abort_server_lifespan(tmp_path) -> None:
    settings = _settings_for_engine(tmp_path)
    engine = _CrashingEngine()
    app = create_server_app(_container(settings, engine))

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(engine.started.wait(), timeout=2)
        # The server keeps serving even though the engine task died; the
        # failure is logged by _run_engine instead of escaping the lifespan.
        await asyncio.sleep(0.01)

    assert engine.shutdown_calls == 1


class _LifespanShipper:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.closed = False

    async def run_forever(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self) -> None:
        self.closed = True


async def test_lifespan_runs_the_audit_shipper_without_the_engine(tmp_path) -> None:
    settings = make_settings(str(tmp_path / "data"))  # run_engine=False
    shipper = _LifespanShipper()
    container = cast(
        AppContainer,
        SimpleNamespace(settings=settings, version="test", engine=None, audit_shipper=shipper),
    )
    app = create_server_app(container)

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(shipper.started.wait(), timeout=2)
        assert shipper.cancelled is False

    assert shipper.cancelled is True
    assert shipper.closed is True
