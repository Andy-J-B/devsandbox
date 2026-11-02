"""Fake resource monitor implementing the ResourceMonitor protocol."""

from __future__ import annotations

from collections.abc import Sequence

from devsandbox.scheduler.interfaces import HostResources, PressureState


def _default_sample() -> HostResources:
    return HostResources(
        cpu_percent=10.0,
        cpu_count=4,
        memory_total_mb=16384.0,
        memory_available_mb=12288.0,
        disk_free_mb=100_000,
        load_avg=(0.2, 0.3, 0.4),
        process_count=3,
    )


class FakeResourceMonitor:
    """Scriptable fake host resource monitor.

    Queued samples are returned in order.  When the queue is exhausted the last
    served sample (or a sane default) is repeated, which keeps multi-round tests
    simple.  Pressures can be scripted independently.
    """

    def __init__(
        self,
        samples: Sequence[HostResources] | None = None,
        pressures: Sequence[PressureState] | None = None,
        *,
        default_sample: HostResources | None = None,
        default_pressure: PressureState = PressureState.OK,
    ) -> None:
        self._queued_samples: list[HostResources] = list(samples or [])
        self._queued_pressures: list[PressureState] = list(pressures or [])
        self._default_sample = default_sample or _default_sample()
        self._current_pressure = default_pressure
        self.samples: list[HostResources] = []
        self.call_count = 0

    def queue(self, sample: HostResources, pressure: PressureState | None = None) -> None:
        """Queue one sample and (optionally) the pressure reported after it."""
        self._queued_samples.append(sample)
        if pressure is not None:
            self._queued_pressures.append(pressure)

    def set_pressure(self, pressure: PressureState) -> None:
        """Set the pressure returned by subsequent calls."""
        self._current_pressure = pressure

    async def sample(self) -> HostResources:
        """Return the next scripted sample."""
        self.call_count += 1
        if self._queued_samples:
            sample = self._queued_samples.pop(0)
        elif self.samples:
            sample = self.samples[-1]
        else:
            sample = self._default_sample
        self.samples.append(sample)
        if self._queued_pressures:
            self._current_pressure = self._queued_pressures.pop(0)
        return sample

    async def pressure_state(self) -> PressureState:
        """Return the currently scripted pressure."""
        if self._queued_pressures:
            self._current_pressure = self._queued_pressures.pop(0)
        return self._current_pressure

    async def history(self, seconds: int | None = None) -> list[HostResources]:
        """Return every sample served so far."""
        return list(self.samples)
