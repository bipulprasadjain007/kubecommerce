"""Message processing and RabbitMQ consumption for the notification worker.

:class:`NotificationProcessor` holds the pure(ish) business logic and receives
every collaborator (Redis, retry/DLQ publishers, webhook, metrics, settings) by
injection, so it can be unit-tested without RabbitMQ. :class:`NotificationConsumer`
owns the robust AMQP connection lifecycle and translates processor outcomes into
manual acks.

Dedupe protocol (see ``docs/development.md`` section 9)::

    SET dedupe:{event_id} processing NX EX <processing_ttl>
    ... side effect ...
    SET dedupe:{event_id} done EX <dedupe_ttl>   # success
    DEL dedupe:{event_id}                        # failure -> a retry can reprocess

The ``processing`` claim and the ``done`` marker use separate TTLs (short vs
long) so a crashed handler cannot pin a key for days. ``done`` is never written
before the side effect succeeds.

An NX conflict whose value is not ``done`` means another delivery is (or was)
in flight: the key is intentionally left untouched and the message is
rescheduled through the normal tiered retry/DLQ path instead of being acked and
lost. Redis errors are treated as transient too: they schedule the same tiered
retry rather than nack-requeueing in a hot loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from aio_pika import DeliveryMode, Message, connect_robust
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)
from fastapi import FastAPI
from prometheus_client import Counter, Histogram
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.topology import declare_topology
from kubecommerce_contracts import (
    HEADER_RETRY_COUNT,
    ROUTING_ORDER_CREATED_DLQ,
    OrderCreatedEvent,
    UnsupportedEventVersionError,
    event_headers,
    parse_event,
    retry_routing_key,
)
from kubecommerce_observability import (
    ServiceMetrics,
    create_http_client,
    get_logger,
    reset_context,
    set_correlation_id,
)

__all__ = [
    "AmqpPublisher",
    "NotificationConsumer",
    "NotificationProcessor",
    "Outcome",
    "WorkerMetrics",
    "build_webhook",
]

Outcome = Literal["processed", "duplicate", "retry", "dlq", "poison"]

PublishRetry = Callable[[bytes, Mapping[str, Any], int], Awaitable[None]]
PublishDlq = Callable[[bytes, Mapping[str, Any]], Awaitable[None]]
Webhook = Callable[[Mapping[str, Any]], Awaitable[None]]

_PROCESSING = "processing"
_DONE = "done"

_logger = get_logger("kubecommerce.notification_worker")


@dataclass(frozen=True, slots=True)
class WorkerMetrics:
    """Bundle of notification-worker-specific metric instruments.

    Created once per application so the same instruments are shared by every
    handler (the Prometheus registry rejects duplicate registrations).
    """

    consumed_total: Counter
    processing_duration_seconds: Histogram
    retry_total: Counter
    dlq_total: Counter
    duplicate_total: Counter
    poison_total: Counter
    webhook_failures_total: Counter

    @classmethod
    def create(cls, metrics: ServiceMetrics) -> WorkerMetrics:
        """Build the worker metrics from the shared service metrics instance."""
        return cls(
            consumed_total=metrics.counter(
                "consumed_total",
                "Messages consumed by processing result.",
                ("result",),
            ),
            processing_duration_seconds=metrics.histogram(
                "processing_duration_seconds",
                "Message processing duration in seconds.",
            ),
            retry_total=metrics.counter(
                "retry_total",
                "Messages rescheduled for retry.",
            ),
            dlq_total=metrics.counter(
                "dlq_total",
                "Messages dead-lettered.",
            ),
            duplicate_total=metrics.counter(
                "duplicate_total",
                "Duplicate messages skipped by the dedupe guard.",
            ),
            poison_total=metrics.counter(
                "poison_total",
                "Unparseable messages routed straight to the DLQ.",
            ),
            webhook_failures_total=metrics.counter(
                "webhook_failures_total",
                "Webhook delivery failures.",
            ),
        )


def build_webhook(url: str, timeout: float) -> Webhook:
    """Build a webhook callable that POSTs the notification payload to ``url``."""

    async def _webhook(payload: Mapping[str, Any]) -> None:
        async with create_http_client(base_url=url, timeout=timeout) as client:
            response = await client.post(url, json=dict(payload))
            response.raise_for_status()

    return _webhook


def _parse_retry_count(headers: Mapping[str, Any]) -> int:
    """Return a non-negative retry count from ``headers`` (default 0)."""
    raw = headers.get(HEADER_RETRY_COUNT, 0)
    if raw is None:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value >= 0 else 0


def _as_text(value: Any) -> str:
    """Normalise a Redis value (bytes or str) to text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class NotificationProcessor:
    """Process one ``order.created`` message and return its outcome.

    Collaborators are injected so tests need no RabbitMQ, Redis server or
    network.
    """

    def __init__(
        self,
        *,
        redis: Redis,
        publish_retry: PublishRetry,
        publish_dlq: PublishDlq,
        webhook: Webhook | None,
        metrics: WorkerMetrics,
        settings: Settings,
    ) -> None:
        self._redis = redis
        self._publish_retry = publish_retry
        self._publish_dlq = publish_dlq
        self._webhook = webhook
        self._metrics = metrics
        self._max_retries = settings.worker_max_retries
        self._processing_ttl = settings.worker_processing_ttl_seconds
        self._dedupe_ttl = settings.worker_dedupe_ttl_seconds

    async def handle(self, message_body: bytes, headers: Mapping[str, Any]) -> Outcome:
        """Handle a raw message body and AMQP headers, returning the outcome."""
        started = time.perf_counter()
        retry_count = _parse_retry_count(headers)
        outcome: Outcome = "retry"
        try:
            outcome = await self._dispatch(message_body, headers, retry_count)
            return outcome
        finally:
            self._metrics.processing_duration_seconds.observe(time.perf_counter() - started)
            self._metrics.consumed_total.labels(result=outcome).inc()

    async def _dispatch(
        self,
        message_body: bytes,
        headers: Mapping[str, Any],
        retry_count: int,
    ) -> Outcome:
        try:
            event = parse_event(message_body)
        except (
            UnsupportedEventVersionError,
            ValidationError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            await self._publish_dlq(message_body, dict(headers))
            self._metrics.poison_total.inc()
            _logger.warning("notification_poison", error=str(exc), retry_count=retry_count)
            return "poison"

        # Propagate the event's correlation id onto outbound webhook calls and
        # into every log record for the duration of this message.
        set_correlation_id(event.correlation_id)
        try:
            return await self._process_event(event, message_body, headers, retry_count)
        finally:
            reset_context()

    async def _process_event(
        self,
        event: OrderCreatedEvent,
        message_body: bytes,
        headers: Mapping[str, Any],
        retry_count: int,
    ) -> Outcome:
        key = f"dedupe:{event.event_id}"
        try:
            claimed = await self._redis.set(key, _PROCESSING, nx=True, ex=self._processing_ttl)
        except RedisError as exc:
            # Redis is a degraded dependency: treat the outage as transient and
            # schedule a tiered retry instead of acking (which would drop the
            # message) or nack-requeueing in a hot loop.
            _logger.warning(
                "notification_dedupe_unavailable",
                event_id=str(event.event_id),
                error=str(exc),
            )
            return await self._schedule_retry(event, message_body, headers, retry_count)

        if not claimed:
            try:
                existing = _as_text(await self._redis.get(key))
            except RedisError as exc:
                _logger.warning(
                    "notification_dedupe_unavailable",
                    event_id=str(event.event_id),
                    error=str(exc),
                )
                return await self._schedule_retry(event, message_body, headers, retry_count)
            if existing == _DONE:
                self._metrics.duplicate_total.inc()
                _logger.info(
                    "notification_duplicate",
                    event_id=str(event.event_id),
                    order_id=str(event.data.order_id),
                )
                return "duplicate"
            # Another delivery holds the in-flight claim. Do not delete the key
            # and do not run the side effect; reschedule through the tiered
            # retry/DLQ path so the message is never silently dropped.
            _logger.info(
                "notification_in_flight",
                event_id=str(event.event_id),
                retry_count=retry_count,
            )
            return await self._schedule_retry(event, message_body, headers, retry_count)

        payload: dict[str, Any] = {
            "event_id": str(event.event_id),
            "order_id": str(event.data.order_id),
            "user_id": str(event.data.user_id),
            "total_cents": event.data.total_cents,
            "correlation_id": event.correlation_id,
        }
        try:
            if self._webhook is not None:
                await self._webhook(payload)
        except Exception as exc:
            self._metrics.webhook_failures_total.inc()
            _logger.error(
                "notification_webhook_failed",
                event_id=str(event.event_id),
                error=str(exc),
            )
            await self._delete_claim(key, event)
            return await self._schedule_retry(event, message_body, headers, retry_count)

        try:
            await self._redis.set(key, _DONE, ex=self._dedupe_ttl)
        except RedisError as exc:
            # The side effect ran but the terminal marker could not be written;
            # retry so at-least-once delivery can converge.
            _logger.warning(
                "notification_dedupe_unavailable",
                event_id=str(event.event_id),
                error=str(exc),
            )
            return await self._schedule_retry(event, message_body, headers, retry_count)
        _logger.info(
            "notification_sent",
            event_id=str(event.event_id),
            order_id=str(event.data.order_id),
            user_id=str(event.data.user_id),
            total_cents=event.data.total_cents,
            correlation_id=event.correlation_id,
        )
        return "processed"

    async def _delete_claim(self, key: str, event: OrderCreatedEvent) -> None:
        """Best-effort removal of the in-flight claim after a handled failure."""
        try:
            await self._redis.delete(key)
        except RedisError as exc:
            # A failed delete is transient; the short processing TTL reclaims
            # the key, and we still schedule the retry below.
            _logger.warning(
                "notification_dedupe_delete_failed",
                event_id=str(event.event_id),
                error=str(exc),
            )

    async def _schedule_retry(
        self,
        event: OrderCreatedEvent,
        message_body: bytes,
        headers: Mapping[str, Any],
        retry_count: int,
    ) -> Outcome:
        """Schedule a tiered retry or dead-letter once retries are exhausted.

        The retry publish is intentionally not guarded: if the broker is also
        unavailable the exception propagates to the consumer's nack path.
        """
        next_retry = retry_count + 1
        if next_retry > self._max_retries:
            await self._publish_dlq(message_body, dict(headers))
            self._metrics.dlq_total.inc()
            _logger.error(
                "notification_dead_lettered",
                event_id=str(event.event_id),
                retry_count=retry_count,
            )
            return "dlq"

        retry_headers: dict[str, Any] = dict(headers)
        retry_headers.update(event_headers(event, retry_count=next_retry))
        await self._publish_retry(message_body, retry_headers, next_retry)
        self._metrics.retry_total.inc()
        _logger.warning(
            "notification_retry_scheduled",
            event_id=str(event.event_id),
            attempt=next_retry,
        )
        return "retry"


class AmqpPublisher:
    """Publisher bound to the events exchange, using publisher confirms.

    ``publish_retry``/``publish_dlq`` are passed to
    :class:`NotificationProcessor`; the exchange handle is (re)bound by
    :class:`NotificationConsumer` as the robust connection comes and goes.
    """

    def __init__(self) -> None:
        self._exchange: AbstractExchange | None = None

    def bind(self, exchange: AbstractExchange | None) -> None:
        """Bind (or clear) the exchange handle used for publishes."""
        self._exchange = exchange

    async def publish(self, routing_key: str, body: bytes, headers: Mapping[str, Any]) -> None:
        """Publish ``body`` persistently and wait for the broker confirm."""
        exchange = self._exchange
        if exchange is None:
            raise RuntimeError("cannot publish: the AMQP exchange is not bound")
        message = Message(
            body=body,
            headers=dict(headers),
            delivery_mode=DeliveryMode.PERSISTENT,
            content_type="application/json",
        )
        await exchange.publish(message, routing_key=routing_key)

    async def publish_retry(
        self,
        body: bytes,
        headers: Mapping[str, Any],
        attempt: int,
    ) -> None:
        """Publish to the retry tier for 1-based ``attempt``."""
        await self.publish(retry_routing_key(attempt), body, headers)

    async def publish_dlq(self, body: bytes, headers: Mapping[str, Any]) -> None:
        """Publish to the dead-letter routing key."""
        await self.publish(ROUTING_ORDER_CREATED_DLQ, body, headers)


class NotificationConsumer:
    """Owns the robust AMQP connection and drives :class:`NotificationProcessor`.

    Connection failures are retried in the background so the HTTP server can
    start (and report not-ready) while the broker is unreachable. The robust
    connection transparently restores channels, queues and consumers after a
    reconnect.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        processor: NotificationProcessor,
        publisher: AmqpPublisher,
        app: FastAPI | None = None,
        reconnect_delay: float = 2.0,
        shutdown_timeout: float = 10.0,
    ) -> None:
        self._settings = settings
        self._processor = processor
        self._publisher = publisher
        self._app = app
        self._reconnect_delay = reconnect_delay
        self._shutdown_timeout = shutdown_timeout
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._queue: AbstractQueue | None = None
        self._consumer_tag: str | None = None
        self._inflight: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        """Start the background supervisor task (returns immediately)."""
        if self._task is None:
            self._task = asyncio.create_task(self._supervise(), name="notification-consumer")

    async def stop(self, shutdown_timeout: float | None = None) -> None:
        """Stop consuming, drain in-flight handlers and close the connection."""
        self._stop_event.set()
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=shutdown_timeout or self._shutdown_timeout)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except Exception as exc:
                _logger.warning("consumer_stop_failed", error=str(exc))
            self._task = None

    async def _supervise(self) -> None:
        """Connect and consume, retrying forever until stopped."""
        while not self._stop_event.is_set():
            try:
                await self._connect_and_consume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.warning("broker_connection_failed", error=str(exc))
            if self._stop_event.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._reconnect_delay)

    async def _connect_and_consume(self) -> None:
        """Open a robust connection, declare topology and consume until stopped."""
        connection = await connect_robust(self._settings.worker_rabbitmq_url)
        channel = await connection.channel(publisher_confirms=True)
        try:
            await channel.set_qos(prefetch_count=self._settings.worker_prefetch)
            topology = await declare_topology(channel)
            self._connection = connection
            self._channel = channel
            self._queue = topology.primary_queue
            self._publisher.bind(topology.exchange)
            self._set_state(connection, channel)
            self._consumer_tag = await topology.primary_queue.consume(self._on_message)
            _logger.info(
                "consumer_started",
                queue=topology.primary_queue.name,
                prefetch=self._settings.worker_prefetch,
            )
            stop_waiter = asyncio.ensure_future(self._stop_event.wait())
            closed_waiter = asyncio.ensure_future(connection.closed())
            try:
                waiters: list[asyncio.Future[Any]] = [stop_waiter, closed_waiter]
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop_waiter.cancel()
        finally:
            await self._teardown(connection, channel)

    async def _teardown(
        self,
        connection: AbstractRobustConnection,
        channel: AbstractChannel,
    ) -> None:
        """Cancel consumption, drain in-flight handlers, then close everything."""
        if self._consumer_tag is not None and self._queue is not None:
            with contextlib.suppress(Exception):
                await self._queue.cancel(self._consumer_tag)
        self._consumer_tag = None
        await self._drain()
        self._publisher.bind(None)
        self._connection = None
        self._channel = None
        self._queue = None
        self._set_state(None, None)
        with contextlib.suppress(Exception):
            await channel.close()
        with contextlib.suppress(Exception):
            await connection.close()

    async def _drain(self) -> None:
        """Await in-flight handlers up to the shutdown timeout."""
        pending = [task for task in self._inflight if not task.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=self._shutdown_timeout)

    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        """Handle one delivery: process, then ack or nack-requeue on error."""
        current = asyncio.current_task()
        if current is not None:
            self._inflight.add(current)
        try:
            try:
                outcome = await self._processor.handle(message.body, dict(message.headers or {}))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.error("message_processing_failed", error=str(exc))
                with contextlib.suppress(Exception):
                    await message.nack(requeue=True)
                return
            await message.ack()
            _logger.info("message_acknowledged", outcome=outcome)
        finally:
            if current is not None:
                self._inflight.discard(current)

    def _set_state(
        self,
        connection: AbstractRobustConnection | None,
        channel: AbstractChannel | None,
    ) -> None:
        """Publish connection handles on ``app.state`` for readiness checks."""
        if self._app is None:
            return
        self._app.state.amqp_connection = connection
        self._app.state.amqp_channel = channel
