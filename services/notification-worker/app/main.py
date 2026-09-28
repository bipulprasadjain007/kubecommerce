"""Application factory for notification-worker.

One process hosts both the RabbitMQ consumer and the health/metrics HTTP
server. The broker connection is established by a background task so the HTTP
server can start (reporting readiness ``false``) while the broker is
unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.consumer import (
    AmqpPublisher,
    NotificationConsumer,
    NotificationProcessor,
    WorkerMetrics,
    build_webhook,
)
from kubecommerce_observability import install_observability, shutdown_tracing


def _is_closed(entity: object) -> bool:
    """Return True when ``entity`` reports itself closed (or is unknown)."""
    return bool(getattr(entity, "is_closed", True))


def create_app(settings: Settings | None = None, *, redis_client: Redis | None = None) -> FastAPI:
    """Create the notification-worker FastAPI application.

    ``redis_client`` is an injection seam for tests (``fakeredis``); production
    code leaves it ``None`` and the client is built from ``WORKER_REDIS_URL``.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        redis = (
            redis_client
            if redis_client is not None
            else aioredis.from_url(settings.worker_redis_url)
        )
        app.state.redis = redis

        webhook = (
            build_webhook(
                settings.worker_webhook_url,
                settings.worker_webhook_timeout_seconds,
            )
            if settings.worker_webhook_url
            else None
        )
        publisher = AmqpPublisher()
        processor = NotificationProcessor(
            redis=redis,
            publish_retry=publisher.publish_retry,
            publish_dlq=publisher.publish_dlq,
            webhook=webhook,
            metrics=app.state.worker_metrics,
            settings=settings,
        )
        consumer = NotificationConsumer(
            settings=settings,
            processor=processor,
            publisher=publisher,
            app=app,
        )
        app.state.consumer = consumer
        await consumer.start()
        try:
            yield
        finally:
            await consumer.stop()
            await redis.aclose()
            shutdown_tracing()

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        lifespan=lifespan,
    )

    async def broker_ready() -> bool:
        connection = getattr(app.state, "amqp_connection", None)
        channel = getattr(app.state, "amqp_channel", None)
        if connection is None or channel is None:
            return False
        return not _is_closed(connection) and not _is_closed(channel)

    # Redis is a degraded dependency: a Redis outage must not fail readiness.
    # The processor already downgrades Redis errors to tiered retries.
    metrics = install_observability(
        app,
        settings=settings,
        readiness_checks=[("rabbitmq", broker_ready)],
        startup_checks=[],
    )
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.worker_metrics = WorkerMetrics.create(metrics)
    app.state.redis = redis_client
    app.state.amqp_connection = None
    app.state.amqp_channel = None
    app.state.consumer = None
    return app


app = create_app()
