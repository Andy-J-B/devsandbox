"""Unit tests for the resource pressure guard."""

from __future__ import annotations

from devsandbox.common.types import ResourceProfile
from devsandbox.scheduler.config import SchedulerConfig
from devsandbox.scheduler.guard import ResourceGuard
from devsandbox.scheduler.models import (
    GuardAction,
    PressureState,
    RunningTask,
)


def running(task_id: str, priority: int, created_order: int = 0) -> RunningTask:
    return RunningTask(
        task_id=task_id,
        project_id="p1",
        priority=priority,
        created_order=created_order,
        resources=ResourceProfile(cpu_cores=1.0),
    )


def test_ok_pressure_continues() -> None:
    decision = ResourceGuard().evaluate(pressure=PressureState.OK)
    assert decision.action is GuardAction.CONTINUE
    assert decision.should_stop_launching is False


def test_elevated_pressure_continues_by_default() -> None:
    decision = ResourceGuard().evaluate(pressure=PressureState.ELEVATED)
    assert decision.action is GuardAction.CONTINUE


def test_elevated_pressure_blocks_when_configured() -> None:
    guard = ResourceGuard(SchedulerConfig(pressure_blocks_elevated=True))
    decision = guard.evaluate(pressure=PressureState.ELEVATED)
    assert decision.action is GuardAction.STOP_LAUNCHING
    assert decision.should_stop_launching is True


def test_critical_pressure_stops_launching() -> None:
    decision = ResourceGuard().evaluate(
        pressure=PressureState.CRITICAL,
        running=[running("t1", priority=1)],
    )
    assert decision.action is GuardAction.STOP_LAUNCHING
    assert decision.victim_task_id is None
    assert decision.event_type == "RESOURCE_PRESSURE"


def test_critical_pressure_preempts_lowest_priority_when_configured() -> None:
    guard = ResourceGuard(SchedulerConfig(cancel_lowest_priority_on_critical=True))
    decision = guard.evaluate(
        pressure=PressureState.CRITICAL,
        running=[
            running("high", priority=9),
            running("low-old", priority=1, created_order=1),
            running("low-new", priority=1, created_order=2),
        ],
    )
    assert decision.action is GuardAction.PREEMPT
    assert decision.victim_task_id == "low-old"


def test_critical_pressure_without_running_tasks_does_not_preempt() -> None:
    guard = ResourceGuard(SchedulerConfig(cancel_lowest_priority_on_critical=True))
    decision = guard.evaluate(pressure=PressureState.CRITICAL)
    assert decision.action is GuardAction.STOP_LAUNCHING
    assert decision.victim_task_id is None
