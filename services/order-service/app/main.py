"""Application factory and lifespan wiring for the order service.

Readiness checks the database only. RabbitMQ is a *degraded* dependency: the
transactional outbox keeps accepting orders while the broker is down and the
poller drains the backlog once it recovers. The broker is never a startup or
readiness blocker.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.api.routes import router
from app.clients.catalog import CatalogClient
from app.clients.publisher import OutboxPublisher, PublishCallable
from app.config import Settings, get_settings
from app.db import check_database, create_engine, create_session_factory
from app.metrics import build_order_metrics, build_outbox_metrics
from app.outbox import OutboxPoller
from kubecommerce_contracts import EXCHANGE_EVENTS, ROUTING_ORDER_CREATED
from kubecommerce_observability import (
    ServiceMetrics,
    create_http_client,
    install_observability,
    shutdown_tracing,
)

_POLLER_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_AMQP_CLOSE_TIMEOUT_SECONDS = 5.0


def create_app(
    settings: Settings | None = None,
    *,
    publish: PublishCallable | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``publish`` lets tests inject a fake broker collaborator; when omitted a
    real, lazily-connected :class:`OutboxPublisher` is used.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.orders_database_url)
        session_factory = create_session_factory(engine)
        http_client = create_http_client(
            base_url=settings.orders_catalog_base_url,
            timeout=settings.orders_request_timeout_seconds,
        )
        catalog = CatalogClient(
            http_client,
            internal_token=settings.orders_internal_api_token,
            metrics=app.state.order_metrics,
        )

        publisher: OutboxPublisher | None = None
        publish_callable: PublishCallable
        if publish is not None:
            publish_callable = publish
        else:
            publisher = OutboxPublisher(
                settings.orders_rabbitmq_url,
                exchange_name=EXCHANGE_EVENTS,
                routing_key=ROUTING_ORDER_CREATED,
            )
            publish_callable = publisher.publish

        poller = OutboxPoller(
            session_factory=session_factory,
            publish=publish_callable,
            interval_seconds=settings.orders_outbox_poll_interval_seconds,
            batch_size=settings.orders_outbox_batch_size,
            dialect=engine.sync_engine.dialect.name,
            metrics=app.state.outbox_metrics,
        )

        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.catalog = catalog
        app.state.http_client = http_client
        app.state.publisher = publisher
        app.state.outbox_poller = poller
        app.state.outbox_task = asyncio.create_task(poller.run(), name="outbox-poller")

        try:
            yield
        finally:
            app.state.outbox_task.cancel()
            with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(
                    app.state.outbox_task,
                    timeout=_POLLER_SHUTDOWN_TIMEOUT_SECONDS,
                )
            if publisher is not None:
                with suppress(Exception):
                    await asyncio.wait_for(
                        publisher.close(),
                        timeout=_AMQP_CLOSE_TIMEOUT_SECONDS,
                    )
            await http_client.aclose()
            await engine.dispose()
            shutdown_tracing()

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        lifespan=lifespan,
    )

    async def _database_ready() -> bool:
        engine = getattr(app.state, "engine", None)
        if engine is None:
            return False
        return await check_database(engine)

    metrics: ServiceMetrics = install_observability(
        app,
        settings=settings,
        readiness_checks=[("database", _database_ready)],
        startup_checks=[],
    )
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.outbox_metrics = build_outbox_metrics(metrics)
    app.state.order_metrics = build_order_metrics(metrics)
    app.include_router(router)
    return app


app = create_app()
