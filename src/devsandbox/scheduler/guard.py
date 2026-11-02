"""Runtime pressure enforcement hook.

The guard only produces a decision (stop launching / preempt one attempt); the
orchestrator performs the state transition and container cleanup.  This keeps
the scheduler free of side effects while still giving plan 08 a single, testable
signal.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from ..common.time import utcnow
from ..db.models import EventModel
from ..db.repositories.event_repo import EventRepo
from .config import SchedulerConfig
from .models import GuardAction, GuardDecision, PressureState, RunningTask


class ResourceGuard:
    """Classify pressure and recommend an orchestration action."""

    def __init__(self, config: SchedulerConfig | None = None) -> None:
        self.config = config or SchedulerConfig()

    def evaluate(
        self,
        *,
        pressure: PressureState,
        running: Sequence[RunningTask] = (),
    ) -> GuardDecision:
        """Return the guard action for the current pressure and running tasks."""
        if pressure is PressureState.CRITICAL:
            victim = self._victim(running) if running else None
            if victim is not None and self.config.cancel_lowest_priority_on_critical:
                return GuardDecision(
                    action=GuardAction.PREEMPT,
                    pressure=pressure,
                    reason=(
                        "host pressure critical; preempt the lowest-priority "
                        f"running task {victim.task_id}"
                    ),
                    victim_task_id=victim.task_id,
                )
            return GuardDecision(
                action=GuardAction.STOP_LAUNCHING,
                pressure=pressure,
                reason="host pressure critical; stop launching new tasks",
            )
        if pressure is PressureState.ELEVATED and self.config.pressure_blocks_elevated:
            return GuardDecision(
                action=GuardAction.STOP_LAUNCHING,
                pressure=pressure,
                reason="host pressure elevated; launching is blocked by policy",
            )
        if pressure is PressureState.ELEVATED:
            return GuardDecision(
                action=GuardAction.CONTINUE,
                pressure=pressure,
                reason="host pressure elevated; no running task needs to be preempted",
            )
        return GuardDecision(
            action=GuardAction.CONTINUE,
            pressure=pressure,
            reason="host pressure ok",
        )

    @staticmethod
    def _victim(running: Sequence[RunningTask]) -> RunningTask | None:
        if not running:
            return None
        # Lowest priority first; oldest created task first on ties; task id is
        # the final deterministic tie breaker.
        return min(running, key=lambda task: (task.priority, task.created_order, task.task_id))

    async def emit(
        self,
        session: AsyncSession,
        *,
        project_id: str,
        decision: GuardDecision,
        task_id: str | None = None,
        attempt_id: str | None = None,
        event_repo: EventRepo | None = None,
    ) -> EventModel:
        """Append a ``RESOURCE_PRESSURE`` audit event for the decision."""
        repo = event_repo or EventRepo(session)
        event = EventModel(
            project_id=project_id,
            task_id=task_id or decision.victim_task_id,
            attempt_id=attempt_id,
            type=decision.event_type,
            payload_json=json.dumps(
                {
                    "action": decision.action.value,
                    "pressure": decision.pressure.value,
                    "reason": decision.reason,
                    "victim_task_id": decision.victim_task_id,
                }
            ),
            at=utcnow(),
        )
        return await repo.add(event)
