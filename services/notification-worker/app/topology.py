"""RabbitMQ topology declaration for the notification worker.

Declares, idempotently, the durable topic exchange, the primary queue, the
tiered retry queues and the dead-letter queue. All names and routing keys come
from :mod:`kubecommerce_contracts` so the worker and the order-service producer
cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractQueue

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


@dataclass(frozen=True, slots=True)
class Topology:
    """Handles to the entities declared on a channel."""

    exchange: AbstractExchange
    primary_queue: AbstractQueue
    retry_queues: tuple[AbstractQueue, ...]
    dlq: AbstractQueue


async def declare_topology(channel: AbstractChannel) -> Topology:
    """Declare the notification topology and return the resulting handles.

    Safe to run on every (re)connect: RabbitMQ declares are idempotent when the
    arguments match.
    """
    exchange = await channel.declare_exchange(EXCHANGE_EVENTS, type="topic", durable=True)

    primary_queue = await channel.declare_queue(
        QUEUE_ORDER_CREATED_NOTIFICATIONS,
        durable=True,
    )
    await primary_queue.bind(exchange, routing_key=ROUTING_ORDER_CREATED)

    retry_queues: list[AbstractQueue] = []
    for index, queue_name in enumerate(QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY):
        retry_queue = await channel.declare_queue(
            queue_name,
            durable=True,
            arguments=retry_queue_arguments(index),
        )
        await retry_queue.bind(exchange, routing_key=retry_routing_key(index + 1))
        retry_queues.append(retry_queue)

    dlq = await channel.declare_queue(
        QUEUE_ORDER_CREATED_NOTIFICATIONS_DLQ,
        durable=True,
    )
    await dlq.bind(exchange, routing_key=ROUTING_ORDER_CREATED_DLQ)

    return Topology(
        exchange=exchange,
        primary_queue=primary_queue,
        retry_queues=tuple(retry_queues),
        dlq=dlq,
    )
