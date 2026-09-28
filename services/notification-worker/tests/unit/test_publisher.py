"""Unit tests for the publisher-confirms AMQP publisher."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aio_pika import DeliveryMode

from app.consumer import AmqpPublisher
from kubecommerce_contracts import (
    ROUTING_ORDER_CREATED_DLQ,
    retry_routing_key,
)


async def test_publish_without_bound_exchange_raises() -> None:
    publisher = AmqpPublisher()
    with pytest.raises(RuntimeError):
        await publisher.publish("order.created", b"{}", {})


async def test_publish_retry_uses_tier_routing_key_and_persistent_delivery() -> None:
    publisher = AmqpPublisher()
    exchange = AsyncMock()
    publisher.bind(exchange)

    await publisher.publish_retry(b'{"a": 1}', {"x-retry-count": "1"}, 1)

    exchange.publish.assert_awaited_once()
    message, kwargs = exchange.publish.await_args.args[0], exchange.publish.await_args.kwargs
    assert kwargs["routing_key"] == retry_routing_key(1)
    assert message.body == b'{"a": 1}'
    assert message.headers == {"x-retry-count": "1"}
    assert message.delivery_mode == DeliveryMode.PERSISTENT


async def test_publish_dlq_uses_dead_letter_routing_key() -> None:
    publisher = AmqpPublisher()
    exchange = AsyncMock()
    publisher.bind(exchange)

    await publisher.publish_dlq(b"{}", {"event_id": "abc"})

    _, kwargs = exchange.publish.await_args.args[0], exchange.publish.await_args.kwargs
    assert kwargs["routing_key"] == ROUTING_ORDER_CREATED_DLQ
