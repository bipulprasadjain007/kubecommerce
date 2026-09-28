"""Redis-backed fixed-window rate limiting with an in-process fail-open path.

The limiter never fails a request because Redis is unavailable. On any Redis
error it logs a warning, increments ``rate_limit_failopen_total`` and falls back
to a bounded in-process counter that is reset every window.
"""

from __future__ import annotations

import math
import time

import redis.asyncio as aioredis
from fastapi import Request
from prometheus_client import Counter

from app.security import authenticate_if_present
from kubecommerce_observability import get_logger

logger = get_logger("gateway.ratelimit")

_WINDOW_SECONDS = 60
_MAX_LOCAL_KEYS = 10_000

_INCR_EXPIRE_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


class RateLimitExceeded(Exception):
    """Raised when a request exceeds the configured fixed-window limit."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limit exceeded")
        self.retry_after = retry_after


class RateLimitResult:
    """Outcome of a single rate limit check."""

    __slots__ = ("allowed", "remaining", "retry_after")

    def __init__(self, allowed: bool, remaining: int, retry_after: int) -> None:
        self.allowed = allowed
        self.remaining = remaining
        self.retry_after = retry_after


class RateLimiter:
    """Fixed-window limiter keyed by identity per minute."""

    def __init__(
        self,
        redis: aioredis.Redis,
        limit: int,
        *,
        exceeded: Counter | None = None,
        failopen: Counter | None = None,
        window_seconds: int = _WINDOW_SECONDS,
        max_local_keys: int = _MAX_LOCAL_KEYS,
    ) -> None:
        self._redis = redis
        self._limit = limit
        self._window_seconds = window_seconds
        self._max_local_keys = max_local_keys
        self._exceeded = exceeded
        self._failopen = failopen
        self._local: dict[str, int] = {}
        self._local_window = -1

    def _window_start(self, now: float) -> int:
        return int(now // self._window_seconds) * self._window_seconds

    def _retry_after(self, now: float) -> int:
        return max(1, math.ceil(self._window_seconds - (now % self._window_seconds)))

    async def check(self, identity: str) -> RateLimitResult:
        """Increment the window counter for ``identity`` and return the outcome."""
        now = time.time()
        retry_after = self._retry_after(now)
        key = f"ratelimit:{self._window_start(now)}:{identity}"
        try:
            current = int(await self._redis.eval(_INCR_EXPIRE_LUA, 1, key, retry_after))
        except Exception as exc:
            logger.warning("rate_limit_redis_unavailable", error_type=type(exc).__name__)
            if self._failopen is not None:
                self._failopen.inc()
            current = self._local_increment(identity, now)

        allowed = current <= self._limit
        if not allowed and self._exceeded is not None:
            self._exceeded.inc()
        return RateLimitResult(
            allowed=allowed,
            remaining=max(0, self._limit - current),
            retry_after=retry_after,
        )

    def _local_increment(self, identity: str, now: float) -> int:
        """Increment the bounded in-process fallback counter for this window."""
        window = int(now // self._window_seconds)
        if window != self._local_window:
            self._local.clear()
            self._local_window = window
        if identity not in self._local and len(self._local) >= self._max_local_keys:
            # Bounded memory: a full fallback map is reset for the window.
            self._local.clear()
        self._local[identity] = self._local.get(identity, 0) + 1
        return self._local[identity]


async def enforce_rate_limit(request: Request) -> None:
    """FastAPI dependency enforcing the rate limit for the current request.

    Identity is the verified token ``sub`` for authenticated requests, or the
    client host otherwise.
    """
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return

    await authenticate_if_present(request)
    identity = getattr(request.state, "user_id", None)
    if not isinstance(identity, str) or not identity:
        client = request.client
        identity = client.host if client is not None else "unknown"

    result = await limiter.check(identity)
    if not result.allowed:
        raise RateLimitExceeded(result.retry_after)
