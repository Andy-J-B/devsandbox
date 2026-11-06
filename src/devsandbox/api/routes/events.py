"""Audit trail and server-sent-events endpoints (plan 10)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import EventModel
from ..errors import ApiError
from ..queries import event_record, load_project
from ..schemas import EventRecord
from ._common import get_container

router = APIRouter(tags=["events"])


async def _events_since(
    session: AsyncSession,
    project_id: str,
    *,
    since_id: str | None,
    limit: int,
) -> list[EventModel]:
    stmt = select(EventModel).where(EventModel.project_id == project_id)
    if since_id:
        marker = await session.get(EventModel, since_id)
        if marker is None or marker.project_id != project_id:
            raise ApiError(
                404,
                "event_not_found",
                f"event {since_id} was not found for project {project_id}",
            )
        stmt = stmt.where(
            or_(
                EventModel.at > marker.at,
                and_(EventModel.at == marker.at, EventModel.id > marker.id),
            )
        )
    stmt = stmt.order_by(EventModel.at, EventModel.id).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/projects/{project_id}/events", response_model=list[EventRecord])
async def list_events(
    request: Request,
    project_id: str,
    since: str | None = None,
    limit: int = 500,
) -> list[EventRecord]:
    if limit < 1 or limit > 5000:
        raise ApiError(422, "invalid_limit", "limit must be between 1 and 5000")
    container = get_container(request)
    async with container.session_factory() as session:
        await load_project(session, project_id)
        events = await _events_since(session, project_id, since_id=since, limit=limit)
        return [event_record(event) for event in events]


def _sse(event: EventRecord) -> str:
    payload = json.dumps(event.model_dump(mode="json"), sort_keys=True)
    return f"id: {event.id}\nevent: {event.type}\ndata: {payload}\n\n"


@router.get("/projects/{project_id}/events/stream")
async def stream_events(
    request: Request,
    project_id: str,
    since: str | None = None,
    poll_seconds: float = 1.0,
    once: bool = False,
) -> StreamingResponse:
    """Poll the append-only event log and stream new rows as SSE.

    ``once=true`` is a test/tooling convenience: existing events are emitted and
    the stream closes.  The default keeps the connection open until the client
    disconnects.
    """
    if poll_seconds <= 0 or poll_seconds > 60:
        raise ApiError(422, "invalid_poll", "poll_seconds must be in (0, 60]")
    container = get_container(request)
    async with container.session_factory() as session:
        await load_project(session, project_id)

    async def generator() -> AsyncIterator[str]:
        last_id = since
        while True:
            async with container.session_factory() as session:
                events = await _events_since(session, project_id, since_id=last_id, limit=500)
            for event in events:
                record = event_record(event)
                last_id = record.id
                yield _sse(record)
            if once:
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(poll_seconds)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
