"""Persist scheduling decisions to the append-only event log.

The scheduler itself stays pure; the orchestrator calls this helper after a
round so every launch and every defer reason is explainable later.
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from ..common.time import utcnow
from ..db.models import EventModel
from ..db.repositories.event_repo import EventRepo
from .models import ScheduleDecision, SchedulingSnapshot

SCHEDULE_DECISION_EVENT = "SCHEDULE_DECISION"


async def record_decisions(
    session: AsyncSession,
    snapshot: SchedulingSnapshot,
    decisions: list[ScheduleDecision],
    *,
    event_repo: EventRepo | None = None,
) -> list[EventModel]:
    """Append one ``SCHEDULE_DECISION`` event per decision."""
    repo = event_repo or EventRepo(session)
    projects = {task.task_id: task.project_id for task in snapshot.ready_tasks}
    at = utcnow()
    events: list[EventModel] = []
    for decision in decisions:
        event = EventModel(
            project_id=projects.get(decision.task_id, "unknown"),
            task_id=decision.task_id,
            type=SCHEDULE_DECISION_EVENT,
            payload_json=json.dumps(
                {
                    "action": decision.action.value,
                    "reason_code": (
                        decision.reason_code.value if decision.reason_code is not None else None
                    ),
                    "reason": decision.reason,
                }
            ),
            at=at,
        )
        events.append(await repo.add(event))
    return events
