"""Audit-log shipping and structured operational events (plan 08/10)."""

from __future__ import annotations

from .models import (
    AUDIT_SCHEMA_VERSION,
    AuditEnvelope,
    AuditSource,
    Severity,
    category_for,
    envelope_from_event,
    severity_for,
)
from .shipper import AuditShipper
from .sinks import (
    AuditShipError,
    AuditSink,
    InMemorySink,
    JsonlFileSink,
    WebhookSink,
)

__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "AuditEnvelope",
    "AuditShipError",
    "AuditShipper",
    "AuditSink",
    "AuditSource",
    "InMemorySink",
    "JsonlFileSink",
    "Severity",
    "WebhookSink",
    "category_for",
    "envelope_from_event",
    "severity_for",
]
