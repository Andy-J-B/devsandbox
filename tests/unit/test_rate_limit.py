"""Token-bucket provider rate limiter tests."""

from __future__ import annotations

import asyncio

import pytest

from devsandbox.common.rate_limit import NoopRateLimiter, TokenBucketRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def test_disabled_limiter_never_sleeps() -> None:
    async def run() -> None:
        limiter = NoopRateLimiter()
        await limiter.acquire()
        assert limiter.enabled is False

    asyncio.run(run())


async def test_burst_is_consumed_then_refilled() -> None:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(
        rate_per_minute=60,
        burst=2,
        clock=clock.clock,
        sleep=clock.sleep,
    )
    await limiter.acquire()
    await limiter.acquire()
    assert clock.sleeps == []
    await limiter.acquire()
    assert clock.sleeps == [pytest.approx(1.0)]


async def test_rate_is_sustained_per_minute() -> None:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(
        rate_per_minute=120,
        burst=1,
        clock=clock.clock,
        sleep=clock.sleep,
    )
    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()
    assert clock.sleeps == [pytest.approx(0.5), pytest.approx(0.5)]


async def test_concurrent_callers_are_serialized() -> None:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(
        rate_per_minute=60,
        burst=1,
        clock=clock.clock,
        sleep=clock.sleep,
    )

    async def one() -> None:
        await limiter.acquire()

    await asyncio.gather(*(one() for _ in range(3)))
    # Two callers had to wait one second each (serialized under the lock).
    assert len(clock.sleeps) == 2
    assert all(delay == pytest.approx(1.0) for delay in clock.sleeps)


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(-1)
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(60, burst=0)
