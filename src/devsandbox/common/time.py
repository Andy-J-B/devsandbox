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


def window_start(now: datetime, window_seconds: int) -> datetime:
    """Floor ``now`` to a fixed window boundary (UTC epoch based).

    ``window_seconds`` is clamped to at least one second.  The returned value
    keeps ``now``'s timezone so it can be stored directly in a timezone-aware
    database column.
    """
    epoch = int(now.timestamp())
    floored = epoch - (epoch % max(1, window_seconds))
    return datetime.fromtimestamp(floored, tz=now.tzinfo)
