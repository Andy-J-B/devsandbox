"""Admission / reservation bookkeeping backed by the attempts table.

There is intentionally no in-memory reservation state.  An attempt is a
reservation while its status is non-terminal, so reservations survive restarts
and are consistent with the rest of the orchestrator.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.errors import SchedulerError
from ..common.ids import TaskId
from ..common.time import utcnow
from ..common.types import ResourceProfile
from ..db.models import AttemptModel, TaskModel
from ..db.repositories.attempt_repo import AttemptRepo
from ..db.repositories.task_repo import TaskRepo
from .models import CommittedResources

# Any status not in this set counts as an active reservation.  Terminal names
# follow the vocabulary used by the orchestrator (plan 08); unknown active
# statuses are treated conservatively as reservations.
TERMINAL_ATTEMPT_STATUSES: frozenset[str] = frozenset(
    {"completed", "succeeded", "failed", "cancelled", "released", "error"}
)
RELEASED_ATTEMPT_STATUS = "released"
RESERVED_ATTEMPT_STATUS = "scheduled"


def _coerce_profile(value: ResourceProfile | dict[str, Any] | None) -> ResourceProfile:
    if value is None:
        return ResourceProfile()
    if isinstance(value, ResourceProfile):
        return value
    try:
        return ResourceProfile.model_validate(value)
    except ValidationError:
        # A corrupt profile must never break scheduling; fall back to defaults.
        return ResourceProfile()


def sum_resource_profiles(
    profiles: Iterable[ResourceProfile | dict[str, Any] | None],
) -> CommittedResources:
    """Sum resource reservations from an iterable of task resource profiles."""
    cpu = 0.0
    memory = 0.0
    disk = 0
    count = 0
    for value in profiles:
        profile = _coerce_profile(value)
        cpu += profile.cpu_cores
        memory += profile.memory_mb
        disk += profile.disk_mb
        count += 1
    return CommittedResources(cpu_cores=cpu, memory_mb=memory, disk_mb=disk, attempts=count)


class AdmissionController:
    """Reserve and release resources via non-terminal ``attempts`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.attempts = AttemptRepo(session)
        self.tasks = TaskRepo(session)

    async def committed(self) -> CommittedResources:
        """Return the sum of resources reserved by non-terminal attempts."""
        stmt = (
            select(AttemptModel.id, TaskModel.resource_profile_json)
            .outerjoin(TaskModel, AttemptModel.task_id == TaskModel.id)
            .where(AttemptModel.status.not_in(TERMINAL_ATTEMPT_STATUSES))
        )
        rows = (await self.session.execute(stmt)).all()
        return sum_resource_profiles(row[1] for row in rows)

    async def reserve(
        self,
        attempt_id: str,
        *,
        task_id: str | None = None,
        number: int | None = None,
        status: str = RESERVED_ATTEMPT_STATUS,
    ) -> CommittedResources:
        """Create (or reuse) an active attempt and return the new totals.

        The operation is idempotent for an already-active attempt.  Reusing a
        terminal attempt id is rejected: attempt ids are deterministic per
        attempt number and must never be reopened.
        """
        attempt = await self.attempts.get(attempt_id)
        if attempt is None:
            if task_id is None or number is None:
                raise SchedulerError("task_id and number are required when reserving a new attempt")
            task = await self.tasks.get(TaskId(task_id))
            if task is None:
                raise SchedulerError(f"cannot reserve attempt for unknown task {task_id}")
            self.session.add(
                AttemptModel(id=attempt_id, task_id=task_id, number=number, status=status)
            )
            await self.session.flush()
        elif attempt.status in TERMINAL_ATTEMPT_STATUSES:
            raise SchedulerError(
                f"attempt {attempt_id} is terminal ({attempt.status}); "
                "a new attempt must be created"
            )
        return await self.committed()

    async def release(self, attempt_id: str) -> CommittedResources:
        """Release a reservation.  Missing or already-terminal attempts are a no-op."""
        attempt = await self.attempts.get(attempt_id)
        if attempt is not None and attempt.status not in TERMINAL_ATTEMPT_STATUSES:
            attempt.status = RELEASED_ATTEMPT_STATUS
            attempt.finished_at = utcnow()
            await self.session.flush()
        return await self.committed()

    async def active_attempt_ids(self) -> list[str]:
        """Return ids of attempts currently holding reservations."""
        stmt = (
            select(AttemptModel.id)
            .where(AttemptModel.status.not_in(TERMINAL_ATTEMPT_STATUSES))
            .order_by(AttemptModel.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def record_peak(
        self,
        attempt_id: str,
        *,
        peak_memory_mb: int | None = None,
        peak_cpu_percent: float | None = None,
    ) -> None:
        """Record observed peak usage back onto the attempt (monotonic maxima)."""
        attempt = await self.attempts.get(attempt_id)
        if attempt is None:
            raise SchedulerError(f"attempt {attempt_id} not found")
        if peak_memory_mb is not None:
            attempt.peak_memory_mb = max(attempt.peak_memory_mb or 0, peak_memory_mb)
        if peak_cpu_percent is not None:
            attempt.peak_cpu_percent = max(float(attempt.peak_cpu_percent or 0.0), peak_cpu_percent)
        await self.session.flush()


class DbCommittedProvider:
    """Async callable adapter used by the resource monitor."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def __call__(self) -> CommittedResources:
        async with self._session_factory() as session:
            return await AdmissionController(session).committed()
