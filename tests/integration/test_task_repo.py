"""Integration tests for task repository CAS transitions."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from devsandbox.common.errors import InvalidTransition, TaskNotFoundError
from devsandbox.common.ids import TaskId
from devsandbox.common.types import TaskStatus
from devsandbox.db.models import TaskModel
from devsandbox.db.repositories.task_repo import TaskRepo


async def _create_task(maker, task_key: str) -> TaskId:
    task_id = task_key
    async with maker() as session:
        task = TaskModel(
            project_id="proj-1",
            plan_id="plan-1",
            key=task_key,
            title="Race task",
            status=TaskStatus.READY.value,
            version=1,
        )
        session.add(task)
        await session.flush()
    return TaskId(task_id)


async def test_transition_ready_to_scheduled(db_session) -> None:
    task = TaskModel(
        project_id="proj-1",
        plan_id="plan-1",
        key="task-1",
        title="T",
        status=TaskStatus.READY.value,
        version=1,
    )
    db_session.add(task)
    await db_session.flush()

    repo = TaskRepo(db_session)
    updated = await repo.transition(
        TaskId(task.id), [TaskStatus.READY], TaskStatus.SCHEDULED, "unit-test"
    )
    assert updated.status == TaskStatus.SCHEDULED.value
    assert updated.version == 2


async def test_transition_rejects_unknown_from_state(db_session) -> None:
    task = TaskModel(
        project_id="proj-1",
        plan_id="plan-1",
        key="task-2",
        title="T",
        status=TaskStatus.READY.value,
        version=1,
    )
    db_session.add(task)
    await db_session.flush()

    repo = TaskRepo(db_session)
    with pytest.raises(InvalidTransition):
        await repo.transition(
            TaskId(task.id), [TaskStatus.PENDING], TaskStatus.SCHEDULED, "unit-test"
        )


async def test_transition_rejects_missing_task(db_session) -> None:
    repo = TaskRepo(db_session)
    with pytest.raises(TaskNotFoundError):
        await repo.transition(
            TaskId("does-not-exist"), [TaskStatus.READY], TaskStatus.SCHEDULED, "unit-test"
        )


async def test_transition_cas_race_single_winner(db_engine) -> None:
    """20 concurrent READY->SCHEDULED transitions must yield exactly one winner."""
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    task_id: TaskId | None = None

    async def create_task() -> None:
        nonlocal task_id
        async with maker() as session:
            task = TaskModel(
                project_id="proj-1",
                plan_id="plan-1",
                key="task-race",
                title="Race task",
                status=TaskStatus.READY.value,
                version=1,
            )
            session.add(task)
            await session.flush()
            task_id = TaskId(task.id)
            await session.commit()

    async def attempt() -> str:
        async with maker() as session:
            repo = TaskRepo(session)
            assert task_id is not None
            try:
                await repo.transition(task_id, [TaskStatus.READY], TaskStatus.SCHEDULED, "race")
                await session.commit()
                return "ok"
            except InvalidTransition:
                await session.rollback()
                return "conflict"

    await create_task()
    outcomes = await asyncio.gather(*(attempt() for _ in range(20)))
    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 19
