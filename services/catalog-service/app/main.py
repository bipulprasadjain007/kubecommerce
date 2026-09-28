"""Application factory for catalog-service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text

from app.api.routes import router
from app.cache import CatalogCache
from app.config import Settings, get_settings
from app.db import create_engine_and_session_factory
from app.metrics import CatalogMetrics
from kubecommerce_observability import install_observability, shutdown_tracing


def create_app(settings: Settings | None = None, *, redis_client: Redis | None = None) -> FastAPI:
    """Create the catalog-service FastAPI application.

    ``redis_client`` is an injection seam for tests (``fakeredis``); production
    code leaves it ``None`` and the client is built from ``CATALOG_REDIS_URL``.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine, session_factory = create_engine_and_session_factory(settings.catalog_database_url)
        redis = (
            redis_client
            if redis_client is not None
            else Redis.from_url(settings.catalog_redis_url, decode_responses=True)
        )
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.redis = redis
        app.state.cache = CatalogCache(
            redis, settings.catalog_cache_ttl_seconds, app.state.catalog_metrics
        )
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()
            shutdown_tracing()

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        lifespan=lifespan,
    )

    async def db_ready() -> bool:
        engine = getattr(app.state, "engine", None)
        if engine is None:
            return False
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    metrics = install_observability(
        app,
        settings=settings,
        readiness_checks=[("database", db_ready)],
        startup_checks=[],
    )
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.catalog_metrics = CatalogMetrics.create(metrics)
    app.include_router(router)
    return app


app = create_app()
