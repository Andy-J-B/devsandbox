"""Audit event vocabulary and helpers for the orchestrator (plan 08)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..common.redaction import redact
from ..common.time import utcnow
from ..db.models import EventModel
from ..db.repositories.event_repo import EventRepo

# Lifecycle / pipeline
ATTEMPT_CREATED = "attempt.created"
ATTEMPT_STARTED = "attempt.started"
ATTEMPT_FINISHED = "attempt.finished"
ATTEMPT_ABANDONED = "attempt.abandoned"
AGENT_STARTED = "agent.started"
AGENT_FINISHED = "agent.finished"
VALIDATION_STARTED = "validation.started"
VALIDATION_FINISHED = "validation.finished"
INTEGRATION_PREPARED = "integration.prepared"
INTEGRATION_FINISHED = "integration.finished"
TASK_RETRY_SCHEDULED = "task.retry_scheduled"
TASK_EXHAUSTED = "task.exhausted"
DEPENDENTS_BLOCKED = "task.dependents_blocked"
TASK_OUTCOME_RECORDED = "task.outcome_recorded"

# Project lifecycle
PROJECT_CREATED = "project.created"
PROJECT_ANALYZED = "project.analyzed"
PROJECT_PLANNED = "project.planned"
PROJECT_APPROVED = "project.approved"
PROJECT_STARTED = "project.started"
PROJECT_PAUSED = "project.paused"
PROJECT_RESUMED = "project.resumed"
PROJECT_CANCELLED = "project.cancelled"
PROJECT_COMPLETED = "project.completed"
PROJECT_FAILED = "project.failed"
PROJECT_MODE_CHANGED = "project.mode_changed"
PROJECT_NEEDS_ATTENTION = "project.needs_attention"
PROJECT_REPLANNED = "project.replanned"

# Scheduling / safety / recovery
SCHEDULE_DECISION = "SCHEDULE_DECISION"
POLICY_DENIED = "POLICY_DENIED"
CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
RESOURCE_PRESSURE = "RESOURCE_PRESSURE"
RECOVERY_ACTION = "RECOVERY_ACTION"
RECOVERY_COMPLETED = "RECOVERY_COMPLETED"
LEASE_ACQUIRED = "LEASE_ACQUIRED"
LEASE_LOST = "LEASE_LOST"
ENGINE_STARTED = "ENGINE_STARTED"
ENGINE_STOPPED = "ENGINE_STOPPED"


async def emit_event(
    session: AsyncSession,
    *,
    project_id: str,
    event_type: str,
    task_id: str | None = None,
    attempt_id: str | None = None,
    payload: dict[str, Any] | None = None,
    event_repo: EventRepo | None = None,
) -> EventModel:
    """Append a redacted audit event to the append-only log."""
    repo = event_repo or EventRepo(session)
    safe_payload = redact(payload or {})
    event = EventModel(
        project_id=project_id,
        task_id=task_id,
        attempt_id=attempt_id,
        type=event_type,
        payload_json=safe_payload,
        at=utcnow(),
    )
    return await repo.add(event)


def event_payload(event: EventModel) -> dict[str, Any]:
    """Return an event payload as a dict (tolerates pre-serialized JSON)."""
    raw = event.payload_json
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return loaded if isinstance(loaded, dict) else {"value": loaded}
    return dict(raw or {})


__all__ = [
    "AGENT_FINISHED",
    "AGENT_STARTED",
    "ATTEMPT_ABANDONED",
    "ATTEMPT_CREATED",
    "ATTEMPT_FINISHED",
    "ATTEMPT_STARTED",
    "CIRCUIT_BREAKER",
    "DEPENDENTS_BLOCKED",
    "ENGINE_STARTED",
    "ENGINE_STOPPED",
    "INTEGRATION_FINISHED",
    "INTEGRATION_PREPARED",
    "LEASE_ACQUIRED",
    "LEASE_LOST",
    "POLICY_DENIED",
    "PROJECT_ANALYZED",
    "PROJECT_APPROVED",
    "PROJECT_CANCELLED",
    "PROJECT_COMPLETED",
    "PROJECT_CREATED",
    "PROJECT_FAILED",
    "PROJECT_MODE_CHANGED",
    "PROJECT_NEEDS_ATTENTION",
    "PROJECT_PAUSED",
    "PROJECT_PLANNED",
    "PROJECT_REPLANNED",
    "PROJECT_RESUMED",
    "PROJECT_STARTED",
    "RECOVERY_ACTION",
    "RECOVERY_COMPLETED",
    "RESOURCE_PRESSURE",
    "SCHEDULE_DECISION",
    "TASK_EXHAUSTED",
    "TASK_OUTCOME_RECORDED",
    "TASK_RETRY_SCHEDULED",
    "VALIDATION_FINISHED",
    "VALIDATION_STARTED",
    "emit_event",
    "event_payload",
]
