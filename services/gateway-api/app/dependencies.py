"""Background monitor exposing degraded-dependency health as metrics.

Redis (rate limiter, deliberately fail-open) and auth-service (JWKS is cached)
are **degraded, not fatal** dependencies: a blip must not remove the gateway
from Service endpoints or couple its rollouts to auth. Their liveness is
surfaced here as the ``dependency_up`` gauge plus transition-only warnings.
"""

from __future__ import annotations

import asyncio

import httpx
import redis.asyncio as aioredis
from prometheus_client import Gauge

from kubecommerce_observability import get_logger

logger = get_logger("gateway.dependencies")

_REDIS = "redis"
_AUTH = "auth"


class DependencyMonitor:
    """Periodically probe Redis and auth-service, updating up/down gauges."""

    def __init__(
        self,
        *,
        redis: aioredis.Redis,
        auth_client: httpx.AsyncClient,
        gauge: Gauge,
        interval_seconds: float = 30.0,
        probe_timeout_seconds: float = 2.0,
    ) -> None:
        self._redis = redis
        self._auth_client = auth_client
        self._gauge = gauge
        self._interval = interval_seconds
        self._timeout = probe_timeout_seconds
        self._last: dict[str, bool | None] = {_REDIS: None, _AUTH: None}

    async def _redis_up(self) -> bool:
        """Return True when Redis answers ``PING``."""
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def _auth_up(self) -> bool:
        """Return True when auth-service ``/health/live`` responds 200."""
        try:
            response = await self._auth_client.get("/health/live", timeout=self._timeout)
        except Exception:
            return False
        return response.status_code == 200

    def _record(self, dependency: str, up: bool) -> None:
        """Set the gauge and log only on a state transition."""
        self._gauge.labels(dependency=dependency).set(1 if up else 0)
        previous = self._last[dependency]
        self._last[dependency] = up
        if previous is None or previous == up:
            return
        if up:
            logger.warning("dependency_up", dependency=dependency)
        else:
            logger.warning("dependency_down", dependency=dependency)

    async def check_once(self) -> dict[str, bool]:
        """Probe every dependency once and update gauges/logs."""
        results = {_REDIS: await self._redis_up(), _AUTH: await self._auth_up()}
        for dependency, up in results.items():
            self._record(dependency, up)
        return results

    async def run(self) -> None:
        """Probe on the configured interval until the task is cancelled."""
        while True:
            await self.check_once()
            await asyncio.sleep(self._interval)
