"""structlog configuration with JSON output and automatic redaction."""

from __future__ import annotations

import logging
from typing import Any, cast

import structlog
from structlog.contextvars import merge_contextvars
from structlog.typing import EventDict, WrappedLogger

from .redaction import redact

_EventDict = EventDict


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, bytes):
        return "[REDACTED]"
    return value


def _redact_processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    redacted = {key: _redact_value(val) for key, val in event_dict.items()}
    redacted["event"] = str(redacted.get("event", "unknown"))
    return redacted


structlog.configure(
    processors=[
        merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_processor,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a configured structlog bound logger for ``name``."""
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
