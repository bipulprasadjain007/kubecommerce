"""Tests for the AMQP publisher (no real broker; connect is monkeypatched)."""

from __future__ import annotations

from typing import Any

import aio_pika
import pytest

from app.clients.publisher import OutboxPublisher
from kubecommerce_contracts import EXCHANGE_EVENTS, ROUTING_ORDER_CREATED


class FakeExchange:
    """Records published messages and the exchange declaration."""

    def __init__(self) -> None:
        self.messages: list[tuple[Any, str, bool]] = []
        self.declared: tuple[str, Any, bool] | None = None

    async def publish(self, message: Any, routing_key: str, mandatory: bool = False) -> bool:
        self.messages.append((message, routing_key, mandatory))
        return True


class FakeChannel:
    """Minimal channel facade around a :class:`FakeExchange`."""

    def __init__(self, exchange: FakeExchange) -> None:
        self.exchange = exchange
        self.is_closed = False
        self.publisher_confirms_calls: list[bool] = []

    async def declare_exchange(
        self, name: str, type_: Any = None, durable: bool = False
    ) -> FakeExchange:
        self.exchange.declared = (name, type_, durable)
        return self.exchange


class FakeConnection:
    """Minimal robust-connection facade."""

    def __init__(self, channel: FakeChannel) -> None:
        self.channel_obj = channel
        self.is_closed = False

    async def channel(self, publisher_confirms: bool = False) -> FakeChannel:
        self.channel_obj.publisher_confirms_calls.append(publisher_confirms)
        return self.channel_obj

    async def close(self) -> None:
        self.is_closed = True


def _install_fake_connection(
    monkeypatch: pytest.MonkeyPatch,
    connection: FakeConnection,
) -> list[str]:
    calls: list[str] = []

    async def fake_connect(url: str, **kwargs: Any) -> FakeConnection:
        calls.append(url)
        return connection

    monkeypatch.setattr("app.clients.publisher.aio_pika.connect_robust", fake_connect)
    return calls


async def test_publish_declares_exchange_with_persistent_confirmed_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange = FakeExchange()
    channel = FakeChannel(exchange)
    connection = FakeConnection(channel)
    calls = _install_fake_connection(monkeypatch, connection)
    publisher = OutboxPublisher(
        "amqp://guest:guest@localhost/",
        exchange_name=EXCHANGE_EVENTS,
        routing_key=ROUTING_ORDER_CREATED,
    )

    await publisher.publish(b"payload", {"event_id": "abc"})

    assert calls == ["amqp://guest:guest@localhost/"]
    assert channel.publisher_confirms_calls == [True]
    assert exchange.declared == (EXCHANGE_EVENTS, aio_pika.ExchangeType.TOPIC, True)
    message, routing_key, mandatory = exchange.messages[0]
    assert message.body == b"payload"
    assert message.headers == {"event_id": "abc"}
    assert message.content_type == "application/json"
    assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert routing_key == ROUTING_ORDER_CREATED
    assert mandatory is True

    # A second publish reuses the established connection.
    await publisher.publish(b"payload-2", {})
    assert len(calls) == 1

    await publisher.close()
    assert connection.is_closed is True


async def test_publish_reconnects_when_channel_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    exchange = FakeExchange()
    channel = FakeChannel(exchange)
    connection = FakeConnection(channel)
    calls = _install_fake_connection(monkeypatch, connection)
    publisher = OutboxPublisher(
        "amqp://guest:guest@localhost/",
        exchange_name=EXCHANGE_EVENTS,
        routing_key=ROUTING_ORDER_CREATED,
    )

    await publisher.publish(b"first", {})
    channel.is_closed = True
    await publisher.publish(b"second", {})

    assert len(calls) == 2
    assert len(exchange.messages) == 2


async def test_close_without_connection_is_noop() -> None:
    publisher = OutboxPublisher(
        "amqp://guest:guest@localhost/",
        exchange_name=EXCHANGE_EVENTS,
        routing_key=ROUTING_ORDER_CREATED,
    )

    await publisher.close()
