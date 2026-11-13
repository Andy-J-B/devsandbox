"""Engine resilience to transient database disconnects (PostgreSQL restart drill)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import OperationalError

from devsandbox.orchestrator.engine import OrchestratorEngine, is_transient_db_error
from devsandbox.orchestrator.models import OrchestratorConfig


def _disconnect() -> OperationalError:
    return OperationalError("select 1", {}, ConnectionResetError("server restarted"))


def _engine(*, base: float = 0.0, maximum: float = 4.0) -> tuple[OrchestratorEngine, list[float]]:
    """Build a bare engine with only the fields used by the retry helper."""
    engine = object.__new__(OrchestratorEngine)
    engine.config = OrchestratorConfig(
        db_reconnect_base_seconds=base,
        db_reconnect_max_seconds=maximum,
    )
    engine._shutdown = False
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    engine._sleep = sleep  # type: ignore[method-assign]
    return engine, delays


def test_modelled_operational_errors_are_transient() -> None:
    assert is_transient_db_error(_disconnect())
    assert is_transient_db_error(ConnectionResetError("connection reset"))
    assert not is_transient_db_error(ValueError("bad input"))


@pytest.mark.asyncio
async def test_retry_helper_recovers_from_transient_disconnects() -> None:
    engine, delays = _engine(base=0.5, maximum=2.0)
    calls = {"count": 0}

    async def operation() -> str:
        calls["count"] += 1
        if calls["count"] < 3:
            raise _disconnect()
        return "ok"

    result = await engine._with_db_reconnect(operation, description="drill")

    assert result == "ok"
    assert calls["count"] == 3
    assert delays == [0.5, 1.0]


@pytest.mark.asyncio
async def test_retry_helper_does_not_hide_programming_errors() -> None:
    engine, delays = _engine()

    async def operation() -> None:
        raise ValueError("not a database disconnect")

    with pytest.raises(ValueError, match="not a database disconnect"):
        await engine._with_db_reconnect(operation, description="drill")
    assert delays == []


@pytest.mark.asyncio
async def test_retry_helper_stops_retrying_during_shutdown() -> None:
    engine, delays = _engine()

    async def operation() -> None:
        engine._shutdown = True
        raise _disconnect()

    with pytest.raises(OperationalError):
        await engine._with_db_reconnect(operation, description="drill")
    assert delays == []


@pytest.mark.asyncio
async def test_retry_delay_is_clamped_to_the_configured_maximum() -> None:
    engine, delays = _engine(base=1.0, maximum=2.0)
    calls = {"count": 0}

    async def operation() -> str:
        calls["count"] += 1
        if calls["count"] < 4:
            raise _disconnect()
        return "ok"

    assert await engine._with_db_reconnect(operation, description="drill") == "ok"
    assert delays == [1.0, 2.0, 2.0]


@pytest.mark.asyncio
async def test_shutdown_does_not_crash_when_the_database_is_still_down() -> None:
    engine, _ = _engine()
    engine._lease_acquired = False
    engine._running = {}
    engine._wake = asyncio.Event()

    async def fail(*args: object, **kwargs: object) -> None:
        raise _disconnect()

    engine._emit_engine_event = fail  # type: ignore[method-assign]
    engine.release_lease = fail  # type: ignore[method-assign]

    # No exception: the engine logs and lets the lease expire.
    await engine.shutdown()
