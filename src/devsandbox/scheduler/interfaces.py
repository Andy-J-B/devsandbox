"""Scheduler and resource monitor protocols (implementation owned by plan 07)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class HostResources(BaseModel):
    """Snapshot of host resource utilization."""

    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    available_memory_mb: float = 0.0
    disk_free_mb: int = 0
    process_count: int = 0


class SchedulingSnapshot(BaseModel):
    """Snapshot used by the scheduler to make decisions."""

    project_id: str
    task_ids: list[str] = []
    running_attempts: int = 0
    queued_tasks: int = 0
    host: HostResources = HostResources()


class ScheduleDecision(BaseModel):
    """A scheduling decision for one task."""

    task_id: str
    action: str = "run"
    reason: str = ""


@runtime_checkable
class Scheduler(Protocol):
    """Protocol implemented by the scheduler."""

    async def select(self, snapshot: SchedulingSnapshot) -> list[ScheduleDecision]: ...


@runtime_checkable
class ResourceMonitor(Protocol):
    """Protocol implemented by the host resource monitor."""

    async def sample(self) -> HostResources: ...
