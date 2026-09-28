"""Rate limiter unit tests."""

from __future__ import annotations

import fakeredis.aioredis
from conftest import BrokenRedis
from prometheus_client import CollectorRegistry, Counter

from app.ratelimit import RateLimiter


def _counter(name: str) -> Counter:
    return Counter(name, "test counter", registry=CollectorRegistry())


async def test_check_allows_then_blocks_and_counts(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    exceeded = _counter("test_rl_exceeded_total")
    limiter = RateLimiter(fake_redis, limit=1, exceeded=exceeded)

    first = await limiter.check("identity")
    second = await limiter.check("identity")

    assert first.allowed is True
    assert first.remaining == 0
    assert second.allowed is False
    assert second.retry_after >= 1
    assert exceeded._value.get() == 1


async def test_failopen_uses_local_counter_and_metrics(broken_redis: BrokenRedis) -> None:
    failopen = _counter("test_rl_failopen_total")
    limiter = RateLimiter(broken_redis, limit=5, failopen=failopen)

    result = await limiter.check("identity")

    assert result.allowed is True
    assert failopen._value.get() == 1


def test_local_fallback_is_bounded_and_resets_per_window(broken_redis: BrokenRedis) -> None:
    limiter = RateLimiter(broken_redis, limit=5, window_seconds=60, max_local_keys=2)

    assert limiter._local_increment("a", now=100.0) == 1
    assert limiter._local_increment("a", now=100.0) == 2
    assert limiter._local_increment("b", now=100.0) == 1

    # A third key while the map is full resets it (bounded memory).
    assert limiter._local_increment("c", now=100.0) == 1
    assert limiter._local == {"c": 1}

    # A new window clears the fallback map.
    assert limiter._local_increment("d", now=200.0) == 1
    assert limiter._local == {"d": 1}
