"""Injectable clock and UTC "now" helper."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

_ClockFn = Callable[[], datetime]


def _real_now() -> datetime:
    return datetime.now(UTC)


_real_now_fn: _ClockFn = _real_now
_overridden: _ClockFn | None = None


def utcnow() -> datetime:
    """Return the current UTC time (timezone-aware). Tests may override via ``set_utcnow``."""
    now_fn = _overridden if _overridden is not None else _real_now_fn
    now = now_fn()
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def set_utcnow(fn: _ClockFn) -> None:
    """Override the clock (mainly for tests)."""
    global _overridden
    _overridden = fn
