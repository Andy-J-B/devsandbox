"""Drill: idle resource use over a configurable observation window.

This catches leaks and busy-loop regressions in the engine/DB loop.  The
default window is 30 seconds; use a longer one for a pre-release soak::

    DEVSANDBOX_DRILLS=1 DEVSANDBOX_DRILL_IDLE_SECONDS=300 \
        python3 -m pytest -m drill tests/drills/test_drill_idle_resources.py
"""

from __future__ import annotations

import asyncio
import os
import time

import psutil
import pytest

from devsandbox.orchestrator.models import OrchestratorConfig

pytestmark = [pytest.mark.drill, pytest.mark.docker, pytest.mark.slow]


async def _wait_for_lease(engine: object, *, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if engine._lease_acquired:  # type: ignore[attr-defined]
            return
        await asyncio.sleep(0.05)
    raise AssertionError("engine did not acquire its lease")


def _cpu_seconds(process: psutil.Process) -> float:
    user, system = process.cpu_times()[:2]
    return float(user + system)


async def test_idle_engine_does_not_spin_or_leak(drill_env: object) -> None:
    env = drill_env
    window = float(os.environ.get("DEVSANDBOX_DRILL_IDLE_SECONDS", "30"))
    max_growth_mb = float(os.environ.get("DEVSANDBOX_DRILL_MAX_RSS_GROWTH_MB", "64"))
    max_cpu_pct = float(os.environ.get("DEVSANDBOX_DRILL_MAX_CPU_PCT", "50"))

    sandbox = env.sandbox()  # type: ignore[attr-defined]
    agent = env.agent(sandbox)  # type: ignore[attr-defined]
    context = env.context(  # type: ignore[attr-defined]
        agent,
        sandbox=sandbox,
        orchestrator_config=OrchestratorConfig(
            tick_seconds=0.1,
            idle_sleep_seconds=0.02,
            shutdown_timeout_seconds=10.0,
        ),
    )
    engine = context.engine
    process = psutil.Process()

    engine_task = asyncio.create_task(engine.run_forever())
    try:
        await _wait_for_lease(engine, timeout=20.0)
        await asyncio.sleep(2.0)  # warm caches and the asyncpg pool

        rss_start = process.memory_info().rss
        cpu_start = _cpu_seconds(process)
        started = time.monotonic()
        await asyncio.sleep(window)
        elapsed = max(time.monotonic() - started, 0.001)
        rss_end = process.memory_info().rss
        cpu_used = max(_cpu_seconds(process) - cpu_start, 0.0)

        growth_mb = (rss_end - rss_start) / (1024 * 1024)
        cpu_pct = cpu_used / elapsed * 100.0

        assert not engine_task.done(), "idle engine exited unexpectedly"
        assert growth_mb <= max_growth_mb, (
            f"idle RSS grew {growth_mb:.1f} MiB in {elapsed:.0f}s (limit {max_growth_mb:.0f} MiB)"
        )
        assert cpu_pct <= max_cpu_pct, (
            f"idle engine used {cpu_pct:.1f}% CPU over {elapsed:.0f}s (limit {max_cpu_pct:.0f}%)"
        )
    finally:
        await engine.request_shutdown()
        await asyncio.wait_for(engine_task, timeout=20.0)
