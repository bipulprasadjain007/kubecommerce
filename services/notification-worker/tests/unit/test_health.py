"""Health, readiness and metrics endpoint tests (no broker required)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app


class BrokenRedis(fakeredis.aioredis.FakeRedis):
    """FakeRedis whose ping always fails, to prove Redis is not a readiness gate."""

    async def ping(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("redis unavailable")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings, redis_client=fakeredis.aioredis.FakeRedis())


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_health_live(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["service"] == "notification-worker"


async def test_readiness_false_when_broker_unavailable(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"rabbitmq": "failed"}


async def test_readiness_true_with_fake_collaborators(app: FastAPI) -> None:
    app.state.amqp_connection = SimpleNamespace(is_closed=False)
    app.state.amqp_channel = SimpleNamespace(is_closed=False)
    async with _client(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"rabbitmq": "ok"}


async def test_readiness_true_when_redis_unreachable(settings: Settings) -> None:
    app = create_app(settings, redis_client=BrokenRedis())
    app.state.amqp_connection = SimpleNamespace(is_closed=False)
    app.state.amqp_channel = SimpleNamespace(is_closed=False)
    async with _client(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"rabbitmq": "ok"}


async def test_readiness_false_when_channel_closed(app: FastAPI) -> None:
    app.state.amqp_connection = SimpleNamespace(is_closed=False)
    app.state.amqp_channel = SimpleNamespace(is_closed=True)
    async with _client(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["rabbitmq"] == "failed"


async def test_metrics_exposes_custom_counters(app: FastAPI) -> None:
    app.state.worker_metrics.consumed_total.labels(result="processed").inc()
    app.state.worker_metrics.dlq_total.inc()
    async with _client(app) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    body: Any = response.text
    assert "consumed_total" in body
    assert "dlq_total" in body
