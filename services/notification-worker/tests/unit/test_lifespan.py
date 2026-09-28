"""Lifespan and consumer-runtime tests with a fake broker (no RabbitMQ)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app
from kubecommerce_contracts import event_headers
from tests.conftest import make_event


class FakeQueue:
    """Minimal AMQP queue double."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.handler: Callable[[Any], Any] | None = None
        self.cancelled_with: str | None = None

    async def bind(self, exchange: object, *, routing_key: str) -> None:
        self.binding = (exchange, routing_key)

    async def consume(self, handler: Callable[[Any], Any]) -> str:
        self.handler = handler
        return f"tag-{self.name}"

    async def cancel(self, tag: str) -> None:
        self.cancelled_with = tag


class FakeExchange:
    """Minimal AMQP exchange double."""

    async def bind(self, queue: object, *, routing_key: str) -> None:
        self.binding = (queue, routing_key)


class FakeChannel:
    """Minimal AMQP channel double that records declared entities."""

    def __init__(self) -> None:
        self.is_closed = False
        self.qos: int | None = None
        self.queues: list[FakeQueue] = []
        self.exchange = FakeExchange()
        self.closed = False

    async def set_qos(self, *, prefetch_count: int) -> None:
        self.qos = prefetch_count

    async def declare_exchange(self, name: str, *, type: str, durable: bool) -> FakeExchange:
        self.exchange_name = name
        self.exchange_args = (type, durable)
        return self.exchange

    async def declare_queue(
        self,
        name: str,
        *,
        durable: bool = True,
        arguments: dict[str, Any] | None = None,
    ) -> FakeQueue:
        queue = FakeQueue(name)
        queue.durable = durable  # type: ignore[attr-defined]
        queue.arguments = arguments  # type: ignore[attr-defined]
        self.queues.append(queue)
        return queue

    async def close(self) -> None:
        self.is_closed = True
        self.closed = True


class FakeConnection:
    """Minimal robust AMQP connection double."""

    def __init__(self) -> None:
        self.is_closed = False
        self.close_calls = 0
        self.channel_obj = FakeChannel()
        self._closed: asyncio.Future[None] = asyncio.Future()

    async def channel(self, *, publisher_confirms: bool = False) -> FakeChannel:
        self.publisher_confirms = publisher_confirms
        return self.channel_obj

    def closed(self) -> asyncio.Future[None]:
        return self._closed

    async def close(self) -> None:
        self.close_calls += 1
        self.is_closed = True
        if not self._closed.done():
            self._closed.set_result(None)


class FakeMessage:
    """Minimal incoming-message double."""

    def __init__(self, body: bytes, headers: dict[str, Any]) -> None:
        self.body = body
        self.headers = headers
        self.acked = 0
        self.nacked = 0
        self.requeue: bool | None = None

    async def ack(self) -> None:
        self.acked += 1

    async def nack(self, *, requeue: bool = False) -> None:
        self.nacked += 1
        self.requeue = requeue


def _connect(connection: FakeConnection) -> Callable[..., Any]:
    async def _connect_robust(url: str, **kwargs: Any) -> FakeConnection:
        return connection

    return _connect_robust


async def _wait_until(predicate: Callable[[], bool], max_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + max_seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.005)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_lifespan_connects_declares_topology_and_closes(
    settings: Settings,
    monkeypatch: Any,
) -> None:
    connection = FakeConnection()
    monkeypatch.setattr("app.consumer.connect_robust", _connect(connection))
    app = create_app(settings, redis_client=fakeredis.aioredis.FakeRedis())

    async with LifespanManager(app):
        await _wait_until(lambda: app.state.amqp_connection is connection)
        channel = connection.channel_obj
        assert channel.qos == settings.worker_prefetch
        assert connection.publisher_confirms is True
        assert [queue.name for queue in channel.queues][:1] == [
            "kubecommerce.notifications.order-created"
        ]
        assert channel.queues[0].handler is not None

        async with _client(app) as client:
            response = await client.get("/health/ready")
        assert response.status_code == 200

    assert connection.close_calls == 1
    assert app.state.amqp_connection is None
    assert app.state.amqp_channel is None
    assert channel.queues[0].cancelled_with == "tag-kubecommerce.notifications.order-created"


async def test_lifespan_starts_http_when_broker_unreachable(
    settings: Settings,
    monkeypatch: Any,
) -> None:
    async def failing_connect(url: str, **kwargs: Any) -> Any:
        raise OSError("broker unreachable")

    monkeypatch.setattr("app.consumer.connect_robust", failing_connect)
    app = create_app(settings, redis_client=fakeredis.aioredis.FakeRedis())

    async with LifespanManager(app):
        async with _client(app) as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")
        assert live.status_code == 200
        assert ready.status_code == 503
        assert ready.json()["checks"]["rabbitmq"] == "failed"


async def test_consumer_acks_processed_message(
    settings: Settings,
    monkeypatch: Any,
) -> None:
    connection = FakeConnection()
    monkeypatch.setattr("app.consumer.connect_robust", _connect(connection))
    app = create_app(settings, redis_client=fakeredis.aioredis.FakeRedis())

    async with LifespanManager(app):
        await _wait_until(lambda: connection.channel_obj.queues[0].handler is not None)
        handler = connection.channel_obj.queues[0].handler
        assert handler is not None
        event = make_event()
        message = FakeMessage(event.model_dump_json().encode(), event_headers(event))
        await handler(message)

    assert message.acked == 1
    assert message.nacked == 0


async def test_consumer_nacks_when_processing_raises(
    settings: Settings,
    monkeypatch: Any,
) -> None:
    connection = FakeConnection()
    monkeypatch.setattr("app.consumer.connect_robust", _connect(connection))
    app = create_app(settings, redis_client=fakeredis.aioredis.FakeRedis())

    async with LifespanManager(app):
        await _wait_until(lambda: connection.channel_obj.queues[0].handler is not None)
        failing = AsyncMock(side_effect=RuntimeError("boom"))
        app.state.consumer._processor = AsyncMock()
        app.state.consumer._processor.handle = failing
        handler = connection.channel_obj.queues[0].handler
        assert handler is not None
        message = FakeMessage(b"{}", {})
        await handler(message)

    assert message.nacked == 1
    assert message.requeue is True
    assert message.acked == 0
