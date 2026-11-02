"""Scheduler and resource monitor protocols (implementation owned by plan 07)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    DecisionAction,
    DeferReason,
    GuardAction,
    GuardDecision,
    HostResources,
    PressureState,
    ReadyTask,
    RunningTask,
    ScheduleDecision,
    SchedulingLimits,
    SchedulingSnapshot,
)

__all__ = [
    "DecisionAction",
    "DeferReason",
    "GuardAction",
    "GuardDecision",
    "HostResources",
    "PressureState",
    "ReadyTask",
    "ResourceMonitor",
    "RunningTask",
    "ScheduleDecision",
    "Scheduler",
    "SchedulingLimits",
    "SchedulingSnapshot",
]


@runtime_checkable
class Scheduler(Protocol):
    """Protocol implemented by the deterministic scheduler."""

    async def select(self, snapshot: SchedulingSnapshot) -> list[ScheduleDecision]: ...


@runtime_checkable
class ResourceMonitor(Protocol):
    """Protocol implemented by the host resource monitor."""

    async def sample(self) -> HostResources: ...

    async def pressure_state(self) -> PressureState: ...

    async def history(self, seconds: int | None = None) -> list[HostResources]: ...
