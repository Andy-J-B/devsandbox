"""Integration tests for DB-backed admission / reservation bookkeeping."""

from __future__ import annotations

import pytest

from devsandbox.common.errors import SchedulerError
from devsandbox.common.types import ResourceProfile
from devsandbox.db.models import AttemptModel, EventModel, TaskModel
from devsandbox.scheduler.admission import (
    AdmissionController,
    sum_resource_profiles,
)
from devsandbox.scheduler.guard import ResourceGuard
from devsandbox.scheduler.models import CommittedResources, GuardDecision, PressureState


async def add_task(
    session,
    task_id: str,
    *,
    cpu: float = 1.0,
    memory: int = 2048,
    disk: int = 4096,
) -> TaskModel:
    task = TaskModel(
        id=task_id,
        project_id="proj-1",
        plan_id="plan-1",
        key=task_id,
        title=f"Task {task_id}",
        resource_profile_json=ResourceProfile(
            cpu_cores=cpu, memory_mb=memory, disk_mb=disk
        ).model_dump(),
    )
    session.add(task)
    await session.flush()
    return task


async def add_attempt(session, attempt_id: str, task_id: str, status: str, number: int = 1):
    attempt = AttemptModel(id=attempt_id, task_id=task_id, number=number, status=status)
    session.add(attempt)
    await session.flush()
    return attempt


async def test_committed_resources_sum_only_non_terminal_attempts(db_session) -> None:
    await add_task(db_session, "task-a", cpu=1.0, memory=1024, disk=2048)
    await add_task(db_session, "task-b", cpu=2.0, memory=2048, disk=4096)
    await add_attempt(db_session, "attempt-a", "task-a", "running")
    await add_attempt(db_session, "attempt-b", "task-b", "completed")
    await add_attempt(db_session, "attempt-c", "task-b", "failed")
    await add_attempt(db_session, "attempt-d", "task-b", "validating", number=2)

    committed = await AdmissionController(db_session).committed()
    assert committed == CommittedResources(cpu_cores=3.0, memory_mb=3072, disk_mb=6144, attempts=2)


async def test_reserve_is_idempotent_and_counts_attempt(db_session) -> None:
    await add_task(db_session, "task-a")
    controller = AdmissionController(db_session)

    first = await controller.reserve("attempt-a", task_id="task-a", number=1)
    second = await controller.reserve("attempt-a", task_id="task-a", number=1)
    assert first == second
    assert first.attempts == 1
    assert first.cpu_cores == 1.0
    assert await controller.active_attempt_ids() == ["attempt-a"]


async def test_reserve_requires_task_for_new_attempt(db_session) -> None:
    controller = AdmissionController(db_session)
    with pytest.raises(SchedulerError):
        await controller.reserve("attempt-a")
    with pytest.raises(SchedulerError):
        await controller.reserve("attempt-a", task_id="missing", number=1)


async def test_reserve_rejects_terminal_attempt(db_session) -> None:
    await add_task(db_session, "task-a")
    await add_attempt(db_session, "attempt-a", "task-a", "completed")
    controller = AdmissionController(db_session)
    with pytest.raises(SchedulerError):
        await controller.reserve("attempt-a")


async def test_release_frees_reservation_and_is_idempotent(db_session) -> None:
    await add_task(db_session, "task-a")
    controller = AdmissionController(db_session)
    await controller.reserve("attempt-a", task_id="task-a", number=1)
    released = await controller.release("attempt-a")
    assert released.attempts == 0
    assert await controller.release("attempt-a") == released
    assert await controller.release("does-not-exist") == released


async def test_record_peak_keeps_monotonic_maxima(db_session) -> None:
    await add_task(db_session, "task-a")
    controller = AdmissionController(db_session)
    await controller.reserve("attempt-a", task_id="task-a", number=1)

    await controller.record_peak("attempt-a", peak_memory_mb=500, peak_cpu_percent=25.0)
    await controller.record_peak("attempt-a", peak_memory_mb=400, peak_cpu_percent=75.0)
    attempt = await controller.attempts.get("attempt-a")
    assert attempt is not None
    assert attempt.peak_memory_mb == 500
    assert float(attempt.peak_cpu_percent or 0.0) == 75.0


async def test_record_peak_unknown_attempt_raises(db_session) -> None:
    with pytest.raises(SchedulerError):
        await AdmissionController(db_session).record_peak("missing", peak_memory_mb=1)


async def test_guard_emit_writes_resource_pressure_event(db_session) -> None:
    decision = GuardDecision(
        action="stop_launching",
        pressure=PressureState.CRITICAL,
        reason="critical",
    )
    event = await ResourceGuard().emit(db_session, project_id="proj-1", decision=decision)
    assert isinstance(event, EventModel)
    assert event.type == "RESOURCE_PRESSURE"
    assert event.project_id == "proj-1"
    assert event.payload_json is not None
    assert "stop_launching" in event.payload_json


def test_sum_resource_profiles_handles_none_and_corrupt_values() -> None:
    total = sum_resource_profiles(
        [
            ResourceProfile(cpu_cores=1.0, memory_mb=100, disk_mb=200),
            None,
            {"cpu_cores": 2.0, "memory_mb": 200, "disk_mb": 300},
            {"not": "a profile"},
        ]
    )
    assert total.attempts == 4
    # Unknown / corrupt profiles fall back to the conservative default profile.
    assert total.cpu_cores == 5.0
    assert total.memory_mb == 4396
    assert total.disk_mb == 8692
