"""Unit tests for the RabbitMQ topology declaration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.topology import declare_topology
from kubecommerce_contracts import (
    EXCHANGE_EVENTS,
    QUEUE_ORDER_CREATED_NOTIFICATIONS,
    QUEUE_ORDER_CREATED_NOTIFICATIONS_DLQ,
    QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY,
    ROUTING_ORDER_CREATED,
    ROUTING_ORDER_CREATED_DLQ,
    retry_queue_arguments,
    retry_routing_key,
)


async def test_declare_topology_declares_exchange_queues_and_bindings() -> None:
    exchange = AsyncMock()
    queues = [AsyncMock() for _ in range(5)]
    channel = MagicMock()
    channel.declare_exchange = AsyncMock(return_value=exchange)
    channel.declare_queue = AsyncMock(side_effect=queues)

    topology = await declare_topology(channel)

    channel.declare_exchange.assert_awaited_once_with(EXCHANGE_EVENTS, type="topic", durable=True)

    declared_names = [call.args[0] for call in channel.declare_queue.await_args_list]
    assert declared_names == [
        QUEUE_ORDER_CREATED_NOTIFICATIONS,
        *QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY,
        QUEUE_ORDER_CREATED_NOTIFICATIONS_DLQ,
    ]

    primary, *retry_queues, dlq = queues
    assert topology.exchange is exchange
    assert topology.primary_queue is primary
    assert topology.retry_queues == tuple(retry_queues)
    assert topology.dlq is dlq

    primary.bind.assert_awaited_once_with(exchange, routing_key=ROUTING_ORDER_CREATED)
    dlq.bind.assert_awaited_once_with(exchange, routing_key=ROUTING_ORDER_CREATED_DLQ)
    for index, retry_queue in enumerate(retry_queues):
        retry_queue.bind.assert_awaited_once_with(
            exchange, routing_key=retry_routing_key(index + 1)
        )
        declared = channel.declare_queue.await_args_list[index + 1]
        assert declared.kwargs["arguments"] == retry_queue_arguments(index)
        assert declared.kwargs["durable"] is True
