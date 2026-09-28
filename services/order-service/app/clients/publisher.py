"""Lazy, reconnecting AMQP publisher with persistent delivery and confirms.

The connection is established on the first publish so a RabbitMQ outage never
blocks application startup. Robust connections/channels reconnect transparently;
a dropped connection is rebuilt on the next publish attempt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection

#: Collaborator contract the outbox poller publishes through.
PublishCallable = Callable[[bytes, dict[str, str]], Awaitable[None]]


class OutboxPublisher:
    """Publishes message bodies to a durable topic exchange with confirms."""

    def __init__(self, url: str, *, exchange_name: str, routing_key: str) -> None:
        self._url = url
        self._exchange_name = exchange_name
        self._routing_key = routing_key
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None
        self._lock = asyncio.Lock()

    async def _ensure_exchange(self) -> AbstractExchange:
        """Return a declared exchange, connecting on first use."""
        if self._exchange is not None and self._channel is not None and not self._channel.is_closed:
            return self._exchange
        async with self._lock:
            if (
                self._exchange is not None
                and self._channel is not None
                and not self._channel.is_closed
            ):
                return self._exchange
            connection = await aio_pika.connect_robust(self._url)
            channel = await connection.channel(publisher_confirms=True)
            exchange = await channel.declare_exchange(
                self._exchange_name,
                aio_pika.ExchangeType.TOPIC,
                durable=True,
            )
            self._connection = connection
            self._channel = channel
            self._exchange = exchange
            return exchange

    async def publish(self, body: bytes, headers: dict[str, str]) -> None:
        """Publish ``body``; returns only once the broker has confirmed it."""
        exchange = await self._ensure_exchange()
        message = aio_pika.Message(
            body=body,
            headers=cast(dict[str, Any], headers),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        await exchange.publish(message, routing_key=self._routing_key, mandatory=True)

    async def close(self) -> None:
        """Close the AMQP connection if one was established."""
        connection = self._connection
        self._connection = None
        self._channel = None
        self._exchange = None
        if connection is not None and not connection.is_closed:
            await connection.close()
