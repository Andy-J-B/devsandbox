"""Unit tests for the psutil-backed resource monitor and pressure hysteresis."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from devsandbox.config.settings import DevSandboxSettings
from devsandbox.scheduler.config import SchedulerConfig
from devsandbox.scheduler.models import CommittedResources, HostResources, PressureState
from devsandbox.scheduler.monitor import PsutilResourceMonitor

MB = 1024 * 1024


class FakePsutil:
    """Small scriptable stand-in for the psutil module."""

    def __init__(
        self,
        *,
        cpu_percent: float = 10.0,
        cpu_count: int = 4,
        memory_total_mb: float = 8192.0,
        memory_available_mb: float = 7000.0,
        disk_free_mb: int = 100_000,
        disk_free_by_path: dict[str, int] | None = None,
    ) -> None:
        self.cpu_percent_value = cpu_percent
        self.cpu_count_value = cpu_count
        self.memory_total_mb = memory_total_mb
        self.memory_available_mb = memory_available_mb
        self.disk_free_mb = disk_free_mb
        self.disk_free_by_path = disk_free_by_path or {}
        self.call_count = 0

    def cpu_percent(self, interval: float | None = None) -> float:
        self.call_count += 1
        return self.cpu_percent_value

    def cpu_count(self, logical: bool = True) -> int:
        return self.cpu_count_value

    def virtual_memory(self) -> SimpleNamespace:
        return SimpleNamespace(
            total=int(self.memory_total_mb * MB),
            available=int(self.memory_available_mb * MB),
        )

    def disk_usage(self, path: str) -> SimpleNamespace:
        free = self.disk_free_by_path.get(path, self.disk_free_mb)
        return SimpleNamespace(total=free * MB * 2, used=free * MB, free=free * MB)

    def getloadavg(self) -> tuple[float, float, float]:
        return (0.5, 0.4, 0.3)

    def pids(self) -> list[int]:
        return [1, 2, 3]


def make_config(**overrides: object) -> SchedulerConfig:
    values: dict[str, object] = {
        "pressure_cpu_pct": 90.0,
        "pressure_mem_pct": 90.0,
        "pressure_elevated_ratio": 0.85,
        "pressure_hysteresis_samples": 2,
        "disk_min_free_mb": 1024,
        "cpu_sample_interval_seconds": 0.0,
        "history_max_samples": 100,
    }
    values.update(overrides)
    return SchedulerConfig(**values)  # type: ignore[arg-type]


def make_monitor(
    fake: FakePsutil,
    tmp_path: Path,
    *,
    config: SchedulerConfig | None = None,
    committed: CommittedResources | None = None,
    clock: object | None = None,
) -> PsutilResourceMonitor:
    async def provider() -> CommittedResources:
        return committed or CommittedResources()

    kwargs: dict[str, object] = {
        "config": config or make_config(),
        "data_root": tmp_path,
        "committed_provider": provider,
        "psutil_module": fake,
    }
    if clock is not None:
        kwargs["clock"] = clock
    return PsutilResourceMonitor(**kwargs)  # type: ignore[arg-type]


async def test_sample_reports_host_values_and_committed_resources(tmp_path: Path) -> None:
    fake = FakePsutil(cpu_percent=25.0, cpu_count=6)
    monitor = make_monitor(
        fake,
        tmp_path,
        committed=CommittedResources(cpu_cores=2.0, memory_mb=1024, disk_mb=4096, attempts=2),
    )
    sample = await monitor.sample()
    assert sample.cpu_percent == 25.0
    assert sample.cpu_count == 6
    assert sample.memory_total_mb == 8192.0
    assert sample.memory_available_mb == 7000.0
    assert sample.disk_free_mb == 100_000
    assert sample.process_count == 3
    assert sample.load_avg == (0.5, 0.4, 0.3)
    assert sample.committed_cpu_cores == 2.0
    assert sample.committed_memory_mb == 1024.0
    assert sample.committed_disk_mb == 4096
    assert sample.sampled_at is not None
    assert fake.call_count == 1


async def test_first_sample_bootstraps_pressure_without_free_launch_window(
    tmp_path: Path,
) -> None:
    monitor = make_monitor(FakePsutil(cpu_percent=99.0), tmp_path)
    assert monitor.pressure is PressureState.OK
    await monitor.sample()
    assert monitor.pressure is PressureState.CRITICAL


async def test_cpu_pressure_uses_two_sample_hysteresis(tmp_path: Path) -> None:
    fake = FakePsutil(cpu_percent=10.0)
    monitor = make_monitor(fake, tmp_path)
    await monitor.sample()
    assert monitor.pressure is PressureState.OK

    fake.cpu_percent_value = 95.0
    await monitor.sample()
    assert monitor.pressure is PressureState.OK
    await monitor.sample()
    assert monitor.pressure is PressureState.CRITICAL

    fake.cpu_percent_value = 10.0
    await monitor.sample()
    assert monitor.pressure is PressureState.CRITICAL
    await monitor.sample()
    assert monitor.pressure is PressureState.OK


async def test_elevated_pressure_band(tmp_path: Path) -> None:
    fake = FakePsutil(cpu_percent=10.0)
    monitor = make_monitor(fake, tmp_path)
    await monitor.sample()
    fake.cpu_percent_value = 80.0  # >= 90 * 0.85 and < 90
    await monitor.sample()
    assert monitor.pressure is PressureState.OK
    await monitor.sample()
    assert monitor.pressure is PressureState.ELEVATED


async def test_memory_threshold_is_critical(tmp_path: Path) -> None:
    monitor = make_monitor(FakePsutil(memory_total_mb=8192.0, memory_available_mb=400.0), tmp_path)
    await monitor.sample()
    assert monitor.pressure is PressureState.CRITICAL


async def test_disk_threshold_is_critical(tmp_path: Path) -> None:
    monitor = make_monitor(FakePsutil(disk_free_mb=100), tmp_path)
    await monitor.sample()
    assert monitor.pressure is PressureState.CRITICAL


async def test_tighter_docker_disk_root_wins(tmp_path: Path) -> None:
    docker_root = tmp_path / "docker"
    docker_root.mkdir()
    fake = FakePsutil(disk_free_by_path={str(tmp_path): 200_000, str(docker_root): 10})
    monitor = PsutilResourceMonitor(
        config=make_config(),
        data_root=tmp_path,
        docker_root=docker_root,
        psutil_module=fake,
    )
    sample = await monitor.sample()
    assert sample.disk_free_mb == 200_000
    assert sample.docker_disk_free_mb == 10
    assert sample.effective_disk_free_mb == 10


async def test_history_can_be_limited_by_age(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    current = [now]

    def clock() -> datetime:
        return current[0]

    monitor = make_monitor(FakePsutil(), tmp_path, clock=clock)
    await monitor.sample()
    current[0] = now + timedelta(seconds=10)
    await monitor.sample()
    current[0] = now + timedelta(seconds=30)
    await monitor.sample()

    all_samples = await monitor.history()
    recent = await monitor.history(seconds=15)
    assert len(all_samples) == 3
    assert len(recent) == 1
    assert recent[0].sampled_at == now + timedelta(seconds=30)


async def test_pressure_state_samples_once_then_reuses_state(tmp_path: Path) -> None:
    fake = FakePsutil()
    monitor = make_monitor(fake, tmp_path)
    assert await monitor.pressure_state() is PressureState.OK
    assert fake.call_count == 1
    assert await monitor.pressure_state() is PressureState.OK
    assert fake.call_count == 1


async def test_background_sampler_can_start_and_stop(tmp_path: Path) -> None:
    monitor = make_monitor(FakePsutil(), tmp_path)
    task = monitor.start_background_sampler(interval_seconds=0.01)
    same = monitor.start_background_sampler(interval_seconds=0.01)
    assert same is task
    await asyncio.sleep(0.05)
    await monitor.stop_background_sampler()
    assert task.done()
    assert len(await monitor.history()) >= 1


async def test_host_resource_availability_is_reservation_aware() -> None:
    sample = HostResources(
        cpu_percent=50.0,
        cpu_count=4,
        memory_total_mb=8192.0,
        memory_available_mb=6000.0,
        committed_cpu_cores=1.0,
        committed_memory_mb=4096.0,
    )
    assert sample.measured_cpu_available_cores == 2.0
    assert sample.available_cpu_cores == 2.0
    assert sample.available_memory_mb == 4096.0
    assert sample.memory_used_percent == (8192.0 - 6000.0) / 8192.0 * 100.0


def test_config_from_settings_and_limits() -> None:
    settings = DevSandboxSettings()
    config = SchedulerConfig.from_settings(settings)
    limits = config.scheduling_limits({"p1": 1})
    assert limits.max_global_concurrency == settings.limits.max_global_concurrency
    assert limits.max_project_concurrency == settings.limits.max_project_concurrency
    assert limits.project_max_concurrency == {"p1": 1}
