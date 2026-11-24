"""Async token-bucket rate limiting for provider calls.

The limiter is deliberately dependency-free and clock/sleep injectable so it
can be tested without real waiting.  A rate of ``0`` means unlimited.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucketRateLimiter:
    """Process-local token bucket shared by calls to one provider.

    ``rate_per_minute`` is the sustained rate and ``burst`` is the bucket
    capacity.  Callers should share one instance per provider (or per
    credential) so concurrent attempts cannot exceed the configured rate.
    """

    def __init__(
        self,
        rate_per_minute: float,
        *,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_minute < 0:
            raise ValueError("rate_per_minute must be >= 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = float(max(burst, 1))
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._updated_at = clock()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """Return whether this limiter actually throttles."""
        return self._rate_per_second > 0.0

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until ``tokens`` may be consumed."""
        if not self.enabled:
            return
        if tokens <= 0:
            return
        async with self._lock:
            self._refill()
            while self._tokens < tokens:
                missing = tokens - self._tokens
                delay = missing / self._rate_per_second
                await self._sleep(delay)
                self._refill()
            self._tokens -= tokens

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated_at)
        self._updated_at = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)


class NoopRateLimiter:
    """Rate limiter used when provider throttling is disabled."""

    enabled = False

    async def acquire(self, tokens: float = 1.0) -> None:
        """Return immediately."""
        del tokens


__all__ = ["NoopRateLimiter", "TokenBucketRateLimiter"]
