"""Smoke tests against the real psutil implementation."""

from __future__ import annotations

from pathlib import Path

from devsandbox.scheduler.config import SchedulerConfig
from devsandbox.scheduler.models import PressureState
from devsandbox.scheduler.monitor import PsutilResourceMonitor


async def test_real_psutil_sample_returns_sane_values(tmp_path: Path) -> None:
    monitor = PsutilResourceMonitor(
        SchedulerConfig(
            cpu_sample_interval_seconds=0.01,
            sampler_interval_seconds=0.01,
            history_max_samples=10,
        ),
        data_root=tmp_path,
    )
    sample = await monitor.sample()
    assert sample.cpu_count >= 1
    assert 0.0 <= sample.cpu_percent <= 100.0
    assert sample.memory_total_mb > 0
    assert 0.0 <= sample.memory_available_mb <= sample.memory_total_mb
    assert sample.disk_free_mb > 0
    assert sample.process_count > 0
    assert sample.sampled_at is not None
    assert len(sample.load_avg) == 3
    assert monitor.pressure in set(PressureState)
    assert await monitor.pressure_state() in set(PressureState)
    history = await monitor.history()
    assert len(history) == 1
