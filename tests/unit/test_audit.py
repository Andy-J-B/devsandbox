"""Audit envelope classification, redaction, and sink tests (plan 08/10)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from tests.api_helpers import make_settings

from devsandbox.api.bootstrap import _build_audit_shipper
from devsandbox.audit.models import (
    AUDIT_SCHEMA_VERSION,
    category_for,
    envelope_from_event,
    severity_for,
)
from devsandbox.audit.sinks import AuditShipError, JsonlFileSink, WebhookSink

AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _envelope(event_type: str = "task.started", payload: dict | None = None):
    return envelope_from_event(
        event_id="evt-1",
        project_id="proj-1",
        task_id="task-1",
        event_type=event_type,
        at=AT,
        payload=payload or {"ok": True},
    )


def test_category_mapping_covers_lifecycle_and_operational_events() -> None:
    assert category_for("project.created") == "project"
    assert category_for("task.started") == "execution"
    assert category_for("attempt.finished") == "execution"
    assert category_for("agent.started") == "execution"
    assert category_for("validation.finished") == "execution"
    assert category_for("integration.prepared") == "execution"
    assert category_for("ENGINE_STARTED") == "engine"
    assert category_for("LEASE_ACQUIRED") == "engine"
    assert category_for("RECOVERY_COMPLETED") == "engine"
    assert category_for("AUDIT_SHIP_FAILED") == "audit"
    assert category_for("SCHEDULE_DECISION") == "operations"
    assert category_for("POLICY_DENIED") == "security"
    assert category_for("something.unknown") == "other"


def test_severity_mapping() -> None:
    assert severity_for("POLICY_DENIED") == "error"
    assert severity_for("PROJECT_FAILED") == "error"
    assert severity_for("RESOURCE_PRESSURE") == "warning"
    assert severity_for("USAGE_MISMATCH") == "warning"
    assert severity_for("task.started") == "info"


def test_envelope_redacts_secrets_and_sets_schema_version() -> None:
    envelope = _envelope(
        "agent.started",
        {"note": "api_key=supersecretvalue", "nested": {"token": "ghp_" + "a" * 30}},
    )
    dumped = envelope.model_dump(mode="json")
    assert dumped["schema_version"] == AUDIT_SCHEMA_VERSION
    assert dumped["category"] == "execution"
    assert dumped["severity"] == "info"
    assert dumped["task_id"] == "task-1"
    text = json.dumps(dumped)
    assert "supersecretvalue" not in text
    assert "ghp_" + "a" * 30 not in text
    assert "[REDACTED]" in text


def test_envelope_truncates_oversized_payloads() -> None:
    envelope = envelope_from_event(
        event_id="evt-2",
        project_id="proj-1",
        event_type="SCHEDULE_DECISION",
        at=AT,
        payload={"blob": "x" * 5_000},
        max_payload_bytes=1_000,
    )
    assert envelope.payload["truncated"] is True
    assert "reason" in envelope.payload
    assert "blob" not in envelope.payload


async def test_jsonl_file_sink_appends_one_record_per_line(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "events.ndjson"
    sink = JsonlFileSink(path)
    await sink.write([_envelope("task.started"), _envelope("task.exited")])
    await sink.write([_envelope("task.finished")])

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert [record["type"] for record in records] == [
        "task.started",
        "task.exited",
        "task.finished",
    ]


async def test_jsonl_file_sink_ignores_empty_batches(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    await JsonlFileSink(path).write([])
    assert not path.exists()


async def test_webhook_sink_retries_transient_status_then_succeeds() -> None:
    statuses = [503, 429, 200]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(statuses.pop(0))

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookSink(
            "http://example.test/audit",
            token=__import__("pydantic").SecretStr("tok"),
            client=client,
            max_retries=3,
            backoff_base_seconds=1.0,
            sleep=sleep,
        )
        await sink.write([_envelope()])

    assert statuses == []
    assert sleeps == [pytest.approx(1.0), pytest.approx(2.0)]


async def test_webhook_sink_does_not_retry_non_retryable_4xx() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookSink(
            "http://example.test/audit",
            client=client,
            max_retries=3,
            sleep=sleep,
        )
        with pytest.raises(AuditShipError, match="401"):
            await sink.write([_envelope()])

    assert len(calls) == 1
    assert sleeps == []


async def test_webhook_sink_retries_transport_errors_then_fails() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("connection refused", request=request)

    async def sleep(delay: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = WebhookSink(
            "http://example.test/audit",
            client=client,
            max_retries=2,
            sleep=sleep,
        )
        with pytest.raises(AuditShipError, match="transport"):
            await sink.write([_envelope()])

    assert len(calls) == 3


def test_build_audit_shipper_is_none_when_disabled(tmp_path: Path) -> None:
    settings = make_settings(str(tmp_path))
    assert _build_audit_shipper(settings, None, tmp_path) is None  # type: ignore[arg-type]


def test_build_audit_shipper_defaults_to_data_root(tmp_path: Path) -> None:
    settings = make_settings(str(tmp_path))
    enabled = settings.audit.model_copy(update={"enabled": True})
    settings = settings.model_copy(update={"audit": enabled})

    shipper = _build_audit_shipper(settings, None, tmp_path)  # type: ignore[arg-type]

    assert shipper is not None
    assert isinstance(shipper._sink, JsonlFileSink)
    assert shipper._sink.path == tmp_path / "audit" / "events.ndjson"
    assert shipper._state_path == tmp_path / "audit" / "cursor.json"


def test_build_audit_shipper_uses_the_webhook_sink(tmp_path: Path) -> None:
    settings = make_settings(str(tmp_path))
    audit = settings.audit.model_copy(
        update={
            "enabled": True,
            "sink": "webhook",
            "webhook_url": "https://example.test/audit",
        }
    )
    settings = settings.model_copy(update={"audit": audit})

    shipper = _build_audit_shipper(settings, None, tmp_path)  # type: ignore[arg-type]

    assert shipper is not None
    assert isinstance(shipper._sink, WebhookSink)
