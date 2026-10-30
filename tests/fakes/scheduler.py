"""Fake resource monitor implementing the ResourceMonitor protocol."""

from __future__ import annotations

from devsandbox.scheduler.interfaces import HostResources


class FakeResourceMonitor:
    """Scriptable fake host resource monitor."""

    def __init__(self) -> None:
        self.samples: list[HostResources] = []

    async def sample(self) -> HostResources:
        """Sample host resources."""
        sample = HostResources(
            cpu_percent=10.0,
            memory_mb=4096.0,
            available_memory_mb=8192.0,
            disk_free_mb=10000,
            process_count=3,
        )
        self.samples.append(sample)
        return sample
