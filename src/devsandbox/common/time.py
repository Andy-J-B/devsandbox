"""Injectable clock and UTC "now" helper."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

_ClockFn = Callable[[], datetime]

_real_now: _ClockFn = lambda: datetime.now(timezone.utc)
_overridden: _ClockFn | None = None


def utcnow() -> datetime:
    """Return the current UTC time (timezone-aware). Tests may override via ``set_utcnow``."""
    now = _overridden() if _overridden is not None else _real_now
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def set_utcnow(fn: _ClockFn) -> None:
    """Override the clock (mainly for tests)."""
    global _overridden
    _overridden = fn
