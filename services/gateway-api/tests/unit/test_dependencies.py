"""Unit tests for the background dependency monitor (fake clients, no network)."""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import httpx
from conftest import BrokenRedis
from prometheus_client import CollectorRegistry, Gauge

from app.dependencies import DependencyMonitor


class FakeAuthClient:
    """Minimal async ``get`` stub for auth-service health probes."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls: list[tuple[str, float | None]] = []

    async def get(
        self,
        url: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors httpx's kwarg
    ) -> httpx.Response:
        self.calls.append((url, timeout))
        return httpx.Response(
            self.status_code, request=httpx.Request("GET", f"http://auth-service{url}")
        )


class RecordingLogger:
    """Captures warning calls so transitions can be asserted."""

    def __init__(self) -> None:
        self.warnings: list[dict[str, Any]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append({"event": event, **kwargs})


def _gauge() -> Gauge:
    return Gauge(
        "dependency_up",
        "test dependency gauge",
        labelnames=("dependency",),
        registry=CollectorRegistry(),
    )


def _value(gauge: Gauge, dependency: str) -> float:
    return gauge.labels(dependency=dependency)._value.get()  # type: ignore[no-any-return]


async def test_check_once_probes_both_dependencies(
    monkeypatch: Any, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    auth = FakeAuthClient()
    gauge = _gauge()
    recorder = RecordingLogger()
    monkeypatch.setattr("app.dependencies.logger", recorder)
    monitor = DependencyMonitor(redis=fake_redis, auth_client=auth, gauge=gauge)

    results = await monitor.check_once()

    assert results == {"redis": True, "auth": True}
    assert _value(gauge, "redis") == 1.0
    assert _value(gauge, "auth") == 1.0
    assert auth.calls[0][0] == "/health/live"
    # First observation is not a transition: no warning is emitted.
    assert recorder.warnings == []


async def test_gauge_tracks_failure_recovery_and_logs_only_transitions(
    monkeypatch: Any, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    auth = FakeAuthClient(status_code=200)
    gauge = _gauge()
    recorder = RecordingLogger()
    monkeypatch.setattr("app.dependencies.logger", recorder)
    monitor = DependencyMonitor(redis=fake_redis, auth_client=auth, gauge=gauge)

    await monitor.check_once()
    assert _value(gauge, "auth") == 1.0
    assert recorder.warnings == []

    auth.status_code = 503
    await monitor.check_once()
    assert _value(gauge, "auth") == 0.0
    assert recorder.warnings == [{"event": "dependency_down", "dependency": "auth"}]

    # Steady-state failure must not repeat the warning.
    await monitor.check_once()
    await monitor.check_once()
    assert _value(gauge, "auth") == 0.0
    assert len(recorder.warnings) == 1

    auth.status_code = 200
    await monitor.check_once()
    assert _value(gauge, "auth") == 1.0
    assert recorder.warnings[-1] == {"event": "dependency_up", "dependency": "auth"}
    assert len(recorder.warnings) == 2


async def test_redis_failure_sets_gauge_zero(monkeypatch: Any, broken_redis: BrokenRedis) -> None:
    auth = FakeAuthClient()
    gauge = _gauge()
    monkeypatch.setattr("app.dependencies.logger", RecordingLogger())
    monitor = DependencyMonitor(redis=broken_redis, auth_client=auth, gauge=gauge)

    results = await monitor.check_once()

    assert results["redis"] is False
    assert _value(gauge, "redis") == 0.0
    assert _value(gauge, "auth") == 1.0


async def test_auth_failure_is_isolated_from_redis(
    monkeypatch: Any, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    auth = FakeAuthClient(status_code=500)
    gauge = _gauge()
    monkeypatch.setattr("app.dependencies.logger", RecordingLogger())
    monitor = DependencyMonitor(redis=fake_redis, auth_client=auth, gauge=gauge)

    results = await monitor.check_once()

    assert results == {"redis": True, "auth": False}
    assert _value(gauge, "redis") == 1.0
    assert _value(gauge, "auth") == 0.0


async def test_run_probes_until_cancelled(
    monkeypatch: Any, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    auth = FakeAuthClient()
    gauge = _gauge()
    monkeypatch.setattr("app.dependencies.logger", RecordingLogger())
    monitor = DependencyMonitor(
        redis=fake_redis, auth_client=auth, gauge=gauge, interval_seconds=3600.0
    )

    task = asyncio.create_task(monitor.run())
    for _ in range(100):
        if auth.calls:
            break
        await asyncio.sleep(0.001)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("monitor task was not cancelled")

    assert _value(gauge, "redis") == 1.0
