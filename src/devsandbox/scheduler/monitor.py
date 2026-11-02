"""Host resource monitor backed by :mod:`psutil`.

The monitor is deliberately I/O-only: it measures the host, adds the
reservation totals supplied by the admission controller (plan 08 wires this to
the DB), classifies pressure, and keeps a short in-memory history for reporting.
It never launches or cancels tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psutil

from ..common.logging import get_logger
from ..common.time import utcnow
from ..config.settings import get_settings
from .config import SchedulerConfig
from .models import CommittedResources, HostResources, PressureState

_log = get_logger("devsandbox.scheduler.monitor")

CommittedProvider = Callable[[], Awaitable[CommittedResources]]


async def _no_committed() -> CommittedResources:
    return CommittedResources()


def _existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


class PsutilResourceMonitor:
    """Concrete :class:`~devsandbox.scheduler.interfaces.ResourceMonitor`."""

    def __init__(
        self,
        config: SchedulerConfig | None = None,
        *,
        data_root: str | Path | None = None,
        docker_root: str | Path | None = None,
        committed_provider: CommittedProvider | None = None,
        psutil_module: Any | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config or SchedulerConfig.from_settings()
        self._data_root = Path(data_root or get_settings().data_root)
        self._docker_root = Path(docker_root) if docker_root is not None else None
        self._committed_provider = committed_provider or _no_committed
        # The module is injectable so tests can exercise threshold logic without
        # touching the real host.
        self._psutil = psutil_module or psutil
        self._clock = clock
        self._history: deque[HostResources] = deque(maxlen=self.config.history_max_samples)
        self._pressure: PressureState | None = None
        self._pending: PressureState | None = None
        self._pending_count = 0
        self._sampler_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ sample
    async def sample(self) -> HostResources:
        """Measure the host, merge reservations, classify pressure, and record."""
        measured = await asyncio.to_thread(self._sample_sync)
        committed = await self._committed_provider()
        sample = measured.model_copy(
            update={
                "committed_cpu_cores": committed.cpu_cores,
                "committed_memory_mb": committed.memory_mb,
                "committed_disk_mb": committed.disk_mb,
            }
        )
        self._record(sample)
        return sample

    def _sample_sync(self) -> HostResources:
        cpu_percent = float(
            self._psutil.cpu_percent(interval=self.config.cpu_sample_interval_seconds)
        )
        memory = self._psutil.virtual_memory()
        try:
            load_avg: tuple[float, float, float] = tuple(self._psutil.getloadavg())
        except (AttributeError, OSError):
            load_avg = (0.0, 0.0, 0.0)
        try:
            process_count = len(self._psutil.pids())
        except (AttributeError, OSError):
            process_count = 0
        disk_free = self._free_mb(self._data_root)
        docker_free = self._free_mb(self._docker_root) if self._docker_root else None
        return HostResources(
            cpu_percent=cpu_percent,
            cpu_count=int(self._psutil.cpu_count(logical=True) or 1),
            memory_total_mb=float(memory.total) / (1024 * 1024),
            memory_available_mb=float(memory.available) / (1024 * 1024),
            disk_free_mb=disk_free,
            docker_disk_free_mb=docker_free,
            load_avg=load_avg,
            process_count=process_count,
            sampled_at=self._clock(),
        )

    def _free_mb(self, path: Path) -> int:
        try:
            usage = self._psutil.disk_usage(str(_existing_path(path)))
        except (AttributeError, OSError):
            return 0
        return int(usage.free // (1024 * 1024))

    # ---------------------------------------------------------------- pressure
    @property
    def pressure(self) -> PressureState:
        """Return the last classified pressure state (``OK`` before sampling)."""
        return self._pressure or PressureState.OK

    async def pressure_state(self) -> PressureState:
        """Return pressure, sampling once when no sample exists yet."""
        if self._pressure is None:
            await self.sample()
        return self.pressure

    def classify(self, sample: HostResources) -> PressureState:
        """Return the raw (hysteresis-free) pressure for one sample."""
        cfg = self.config
        mem_pct = sample.memory_used_percent
        disk_free = sample.effective_disk_free_mb
        if (
            sample.cpu_percent >= cfg.pressure_cpu_pct
            or mem_pct >= cfg.pressure_mem_pct
            or disk_free < cfg.disk_min_free_mb
        ):
            return PressureState.CRITICAL
        elevated_cpu = cfg.pressure_cpu_pct * cfg.pressure_elevated_ratio
        elevated_mem = cfg.pressure_mem_pct * cfg.pressure_elevated_ratio
        if sample.cpu_percent >= elevated_cpu or mem_pct >= elevated_mem:
            return PressureState.ELEVATED
        return PressureState.OK

    def _apply_hysteresis(self, raw: PressureState) -> PressureState:
        # Bootstrap with the first real measurement so a host that is already
        # critical does not get a free launch window.
        if self._pressure is None:
            self._pressure = raw
            self._pending = raw
            self._pending_count = 0
            return raw
        if raw is self._pressure:
            self._pending = raw
            self._pending_count = 0
            return self._pressure
        if raw is self._pending:
            self._pending_count += 1
        else:
            self._pending = raw
            self._pending_count = 1
        if self._pending_count >= self.config.pressure_hysteresis_samples:
            self._pressure = raw
            self._pending_count = 0
        return self._pressure

    def _record(self, sample: HostResources) -> None:
        self._history.append(sample)
        self._apply_hysteresis(self.classify(sample))

    # ----------------------------------------------------------------- history
    async def history(self, seconds: int | None = None) -> list[HostResources]:
        """Return samples from the ring buffer (optionally limited by age)."""
        if seconds is None:
            return list(self._history)
        cutoff = self._clock() - timedelta(seconds=seconds)
        return [
            sample
            for sample in self._history
            if sample.sampled_at is None or sample.sampled_at >= cutoff
        ]

    # ------------------------------------------------------- background sampler
    def start_background_sampler(self, interval_seconds: float | None = None) -> asyncio.Task[None]:
        """Start periodically sampling into the history buffer."""
        existing = self._sampler_task
        if existing is not None and not existing.done():
            return existing
        interval = interval_seconds or self.config.sampler_interval_seconds

        async def _loop() -> None:
            while True:
                try:
                    await self.sample()
                except Exception:  # pragma: no cover - defensive loop guard
                    _log.exception("resource.sample_failed")
                await asyncio.sleep(interval)

        self._sampler_task = asyncio.create_task(_loop(), name="devsandbox-resource-sampler")
        return self._sampler_task

    async def stop_background_sampler(self) -> None:
        """Cancel the background sampler if one is running."""
        task = self._sampler_task
        self._sampler_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def __aenter__(self) -> PsutilResourceMonitor:
        self.start_background_sampler()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop_background_sampler()
