"""At-least-once audit event shipping with a durable cursor.

The shipper reads the append-only ``events`` table in ``(at, id)`` order and
hands batches to a sink.  The cursor is persisted only after the sink accepts
a batch, so a crash can duplicate the last batch but cannot skip events.  This
is the same recovery trade-off as an outbox without adding another table.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..common.logging import get_logger
from ..common.time import utcnow
from ..db.models import EventModel
from .models import AuditEnvelope, envelope_from_event
from .sinks import AuditShipError, AuditSink

_log = get_logger("devsandbox.audit")


class AuditShipper:
    """Export audit events to an operator sink."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sink: AuditSink,
        *,
        state_path: str | Path,
        batch_size: int = 200,
        interval_seconds: float = 5.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        max_payload_bytes: int = 64_000,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._session_factory = session_factory
        self._sink = sink
        self._state_path = Path(state_path)
        self._batch_size = batch_size
        self._interval_seconds = interval_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_payload_bytes = max_payload_bytes
        self._clock = clock
        self._sleep = sleep
        self._last_at: datetime | None = None
        self._last_id: str | None = None
        self._shipped = 0
        self._last_error: str | None = None
        self._load_cursor()

    async def run_once(self) -> int:
        """Ship one batch and return the number of events written."""
        async with self._session_factory() as session:
            events = await self._next_events(session)
        if not events:
            return 0
        envelopes = [self._envelope(event) for event in events]
        await self._ship(envelopes)
        last = events[-1]
        self._last_at = last.at
        self._last_id = last.id
        self._shipped += len(envelopes)
        self._last_error = None
        self._save_cursor()
        _log.info(
            "audit.shipped",
            count=len(envelopes),
            last_event_id=last.id,
            total_shipped=self._shipped,
        )
        return len(envelopes)

    async def run_forever(self) -> None:
        """Poll for new events until cancelled."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                _log.error(
                    "audit.ship_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    last_event_id=self._last_id,
                )
            await self._sleep(self._interval_seconds)

    def status(self) -> dict[str, Any]:
        """Return a small status document for ``/v1/system/status``."""
        return {
            "enabled": True,
            "shipped": self._shipped,
            "last_event_at": self._last_at.isoformat() if self._last_at else None,
            "last_event_id": self._last_id,
            "last_error": self._last_error,
        }

    async def aclose(self) -> None:
        """Close a sink that owns an HTTP client."""
        close = getattr(self._sink, "aclose", None)
        if close is not None:
            await close()

    async def _next_events(self, session: AsyncSession) -> list[EventModel]:
        stmt = select(EventModel).order_by(EventModel.at, EventModel.id).limit(self._batch_size)
        if self._last_at is not None:
            if self._last_id is None:
                stmt = stmt.where(EventModel.at > self._last_at)
            else:
                stmt = stmt.where(
                    or_(
                        EventModel.at > self._last_at,
                        and_(
                            EventModel.at == self._last_at,
                            EventModel.id > self._last_id,
                        ),
                    )
                )
        return list((await session.execute(stmt)).scalars().all())

    def _envelope(self, event: EventModel) -> AuditEnvelope:
        return envelope_from_event(
            event_id=event.id,
            project_id=event.project_id,
            task_id=event.task_id,
            attempt_id=event.attempt_id,
            event_type=event.type,
            at=event.at,
            payload=_payload(event),
            max_payload_bytes=self._max_payload_bytes,
        )

    async def _ship(self, envelopes: list[AuditEnvelope]) -> None:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                await self._sink.write(envelopes)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < self._max_retries:
                    _log.warning(
                        "audit.ship_retry",
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        error_type=type(exc).__name__,
                    )
                    await self._sleep(self._retry_backoff_seconds * (2**attempt))
        raise AuditShipError(str(last_error) if last_error is not None else "audit sink failed")

    def _load_cursor(self) -> None:
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            at = raw.get("at")
            event_id = raw.get("id")
            self._last_at = datetime.fromisoformat(at) if at else None
            self._last_id = str(event_id) if event_id else None
        except (OSError, ValueError, TypeError, AttributeError):
            # A corrupt cursor must not stop the service.  Re-sending is safe;
            # skipping is not, so restart from the beginning.
            _log.warning("audit cursor is unreadable; restarting from the first event")
            self._last_at = None
            self._last_id = None

    def _save_cursor(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(self._state_path.name + ".tmp")
        payload = {
            "at": self._last_at.isoformat() if self._last_at else None,
            "id": self._last_id,
            "saved_at": self._clock().isoformat(),
        }
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(self._state_path)


def _payload(event: EventModel) -> dict[str, Any]:
    raw = event.payload_json
    if isinstance(raw, str):
        with contextlib.suppress(json.JSONDecodeError):
            loaded = json.loads(raw)
            return loaded if isinstance(loaded, dict) else {"value": loaded}
        return {"raw": raw}
    return dict(raw or {})


__all__ = ["AuditShipper"]
