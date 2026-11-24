"""Structured audit envelopes and operational classification.

The database event row is the source of truth.  A shipped envelope adds a
stable schema version, a coarse category/severity, and source metadata so
external SIEM/log pipelines do not have to parse DevSandbox-specific payloads.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..common.redaction import redact

AUDIT_SCHEMA_VERSION = "1"

Severity = Literal["info", "warning", "error"]


class AuditSource(BaseModel):
    """Host/process metadata attached to every shipped record."""

    model_config = ConfigDict(frozen=True)

    host: str
    pid: int
    component: str = "devsandbox"

    @classmethod
    def current(cls) -> AuditSource:
        """Return this process's source identity."""
        return cls(host=socket.gethostname(), pid=os.getpid())


class AuditEnvelope(BaseModel):
    """One normalized, redacted audit record."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = AUDIT_SCHEMA_VERSION
    id: str
    project_id: str
    task_id: str | None = None
    attempt_id: str | None = None
    type: str
    category: str = "other"
    severity: Severity = "info"
    at: datetime
    source: AuditSource = Field(default_factory=AuditSource.current)
    payload: dict[str, Any] = Field(default_factory=dict)


_CATEGORY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("project.", "project"),
    ("task.", "execution"),
    ("attempt.", "execution"),
    ("agent.", "execution"),
    ("validation.", "execution"),
    ("integration.", "execution"),
    ("ENGINE_", "engine"),
    ("LEASE_", "engine"),
    ("RECOVERY_", "engine"),
    ("AUDIT_", "audit"),
)

_CATEGORY_TYPES: dict[str, str] = {
    "SCHEDULE_DECISION": "operations",
    "RESOURCE_PRESSURE": "operations",
    "CIRCUIT_BREAKER": "operations",
    "STORAGE_GC_COMPLETED": "operations",
    "USAGE_MISMATCH": "operations",
    "POLICY_DENIED": "security",
}

_ERROR_TYPES = {
    "POLICY_DENIED",
    "CIRCUIT_BREAKER",
    "TASK_EXHAUSTED",
    "PROJECT_FAILED",
    "AUDIT_SHIP_FAILED",
}
_WARNING_TYPES = {
    "RESOURCE_PRESSURE",
    "USAGE_MISMATCH",
    "TASK_RETRY_SCHEDULED",
    "PROJECT_NEEDS_ATTENTION",
    "AUDIT_SHIP_RETRY",
}


def category_for(event_type: str) -> str:
    """Return a coarse category for an event type."""
    if event_type in _CATEGORY_TYPES:
        return _CATEGORY_TYPES[event_type]
    for prefix, category in _CATEGORY_PREFIXES:
        if event_type.startswith(prefix):
            return category
    return "other"


def severity_for(event_type: str) -> Severity:
    """Return the operational severity for an event type."""
    if event_type in _ERROR_TYPES:
        return "error"
    if event_type in _WARNING_TYPES:
        return "warning"
    return "info"


def envelope_from_event(
    *,
    event_id: str,
    project_id: str,
    event_type: str,
    at: datetime,
    payload: dict[str, Any] | None,
    task_id: str | None = None,
    attempt_id: str | None = None,
    max_payload_bytes: int = 64_000,
    source: AuditSource | None = None,
) -> AuditEnvelope:
    """Build a redacted, size-bounded envelope from an event row."""
    safe_payload = redact(payload or {})
    if _payload_size(safe_payload) > max_payload_bytes:
        safe_payload = {
            "truncated": True,
            "reason": f"payload exceeded {max_payload_bytes} bytes",
        }
    return AuditEnvelope(
        id=event_id,
        project_id=project_id,
        task_id=task_id,
        attempt_id=attempt_id,
        type=event_type,
        category=category_for(event_type),
        severity=severity_for(event_type),
        at=at,
        source=source or AuditSource.current(),
        payload=safe_payload,
    )


def _payload_size(payload: dict[str, Any]) -> int:
    try:
        return len(json.dumps(payload, default=str, separators=(",", ":")).encode())
    except (TypeError, ValueError):
        return 0


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "AuditEnvelope",
    "AuditSource",
    "Severity",
    "category_for",
    "envelope_from_event",
    "severity_for",
]
