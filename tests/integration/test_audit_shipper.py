"""Audit shipping cursor, resume, and failure semantics (plan 08/10)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from devsandbox.audit.models import AuditEnvelope
from devsandbox.audit.shipper import AuditShipper
from devsandbox.audit.sinks import AuditShipError, InMemorySink
from devsandbox.db.models import EventModel

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


async def _seed(maker: async_sessionmaker, count: int, *, start: int = 0) -> None:
    async with maker() as session:
        for index in range(start, start + count):
            session.add(
                EventModel(
                    id=f"evt-{index:03d}",
                    project_id="proj-1",
                    task_id="task-1",
                    type="task.started",
                    payload_json={
                        "index": index,
                        "api_key": "ghp_" + "a" * 30,
                    },
                    at=START + timedelta(seconds=index),
                )
            )
        await session.commit()


def _shipper(
    maker: async_sessionmaker,
    sink: InMemorySink,
    state_path: Path,
    **kwargs: int,
) -> AuditShipper:
    return AuditShipper(
        maker,
        sink,
        state_path=state_path,
        batch_size=kwargs.get("batch_size", 2),
        interval_seconds=1.0,
        max_retries=kwargs.get("max_retries", 0),
        retry_backoff_seconds=0.0,
    )


async def test_run_once_ships_batches_in_order_without_duplicates(
    db_engine, tmp_path: Path
) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    await _seed(maker, 3)
    sink = InMemorySink()
    shipper = _shipper(maker, sink, tmp_path / "cursor.json")

    assert await shipper.run_once() == 2
    assert await shipper.run_once() == 1
    assert await shipper.run_once() == 0

    shipped = [envelope for batch in sink.batches for envelope in batch]
    assert [envelope.id for envelope in shipped] == ["evt-000", "evt-001", "evt-002"]
    assert all(envelope.category == "execution" for envelope in shipped)
    assert shipper.status()["shipped"] == 3
    assert shipper.status()["last_event_id"] == "evt-002"


async def test_envelopes_are_redacted_and_carry_source_metadata(db_engine, tmp_path: Path) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    await _seed(maker, 1)
    sink = InMemorySink()
    shipper = _shipper(maker, sink, tmp_path / "cursor.json")

    await shipper.run_once()

    envelope: AuditEnvelope = sink.batches[0][0]
    assert envelope.payload["index"] == 0
    assert envelope.payload["api_key"] == "[REDACTED]"
    assert envelope.source.pid > 0
    assert envelope.schema_version == "1"


async def test_cursor_resumes_across_shipper_restarts(db_engine, tmp_path: Path) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    await _seed(maker, 2)
    state_path = tmp_path / "cursor.json"
    first_sink = InMemorySink()
    first = _shipper(maker, first_sink, state_path)
    assert await first.run_once() == 2

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["id"] == "evt-001"

    await _seed(maker, 2, start=2)
    second_sink = InMemorySink()
    second = _shipper(maker, second_sink, state_path)
    assert await second.run_once() == 2  # evt-002 and evt-003 are new
    assert [envelope.id for envelope in second_sink.batches[0]] == ["evt-002", "evt-003"]

    # A fresh shipper does not re-ship the already-delivered events.
    third_sink = InMemorySink()
    third = _shipper(maker, third_sink, state_path)
    assert await third.run_once() == 0
    assert third_sink.batches == []


async def test_sink_failure_leaves_cursor_unchanged(db_engine, tmp_path: Path) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    await _seed(maker, 2)
    state_path = tmp_path / "cursor.json"
    flaky = InMemorySink(fail_times=1)
    shipper = _shipper(maker, flaky, state_path, max_retries=0)

    with pytest.raises(AuditShipError):
        await shipper.run_once()
    assert not state_path.exists()
    assert shipper.status()["shipped"] == 0

    # The next pass re-reads from the beginning and succeeds.
    assert await shipper.run_once() == 2
    assert [envelope.id for batch in flaky.batches for envelope in batch] == [
        "evt-000",
        "evt-001",
    ]
    assert state_path.exists()


async def test_corrupt_cursor_restarts_from_the_beginning(db_engine, tmp_path: Path) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    await _seed(maker, 1)
    state_path = tmp_path / "cursor.json"
    state_path.write_text("not json", encoding="utf-8")
    sink = InMemorySink()
    shipper = _shipper(maker, sink, state_path)

    assert await shipper.run_once() == 1
    assert sink.batches[0][0].id == "evt-000"


async def test_equal_timestamps_use_id_as_the_tie_breaker(db_engine, tmp_path: Path) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with maker() as session:
        for event_id in ("evt-b", "evt-a", "evt-c"):
            session.add(
                EventModel(
                    id=event_id,
                    project_id="proj-1",
                    type="task.started",
                    payload_json={},
                    at=START,
                )
            )
        await session.commit()
    sink = InMemorySink()
    shipper = _shipper(maker, sink, tmp_path / "cursor.json", batch_size=1)

    while await shipper.run_once():
        pass

    assert [envelope.id for batch in sink.batches for envelope in batch] == [
        "evt-a",
        "evt-b",
        "evt-c",
    ]
