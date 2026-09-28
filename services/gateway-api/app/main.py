"""Gateway application factory."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import Settings, get_settings
from app.dependencies import DependencyMonitor
from app.ratelimit import RateLimiter, RateLimitExceeded
from app.security import JWKSVerifier
from kubecommerce_observability import (
    create_http_client,
    error_response,
    install_observability,
    shutdown_tracing,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the gateway FastAPI application."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        timeout = settings.gateway_request_timeout_seconds
        app.state.auth_client = create_http_client(
            base_url=settings.gateway_auth_base_url, timeout=timeout
        )
        app.state.catalog_client = create_http_client(
            base_url=settings.gateway_catalog_base_url, timeout=timeout
        )
        app.state.order_client = create_http_client(
            base_url=settings.gateway_order_base_url, timeout=timeout
        )
        app.state.redis = aioredis.from_url(settings.gateway_redis_url, decode_responses=True)
        app.state.jwks_verifier = JWKSVerifier(
            client=app.state.auth_client,
            jwks_url=settings.gateway_jwks_url,
            issuer=settings.gateway_jwt_issuer,
            audience=settings.gateway_jwt_audience,
            cache_ttl=settings.gateway_jwks_cache_ttl_seconds,
        )
        app.state.rate_limiter = RateLimiter(
            app.state.redis,
            settings.gateway_rate_limit_per_minute,
            exceeded=app.state.rate_limit_exceeded,
            failopen=app.state.rate_limit_failopen,
        )
        monitor = DependencyMonitor(
            redis=app.state.redis,
            auth_client=app.state.auth_client,
            gauge=app.state.dependency_gauge,
            interval_seconds=settings.gateway_dependency_check_interval_seconds,
        )
        monitor_task = asyncio.create_task(monitor.run())
        try:
            yield
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            await app.state.auth_client.aclose()
            await app.state.catalog_client.aclose()
            await app.state.order_client.aclose()
            await app.state.redis.aclose()
            shutdown_tracing()

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Redis (fail-open rate limiter) and auth-service (cached JWKS) are degraded
    # dependencies, not readiness gates: a blip must not eject the gateway from
    # Service endpoints or couple its rollouts to auth. Their health is exposed
    # via the ``dependency_up`` gauge by a background monitor instead.
    metrics = install_observability(
        app,
        settings=settings,
        readiness_checks=[],
        startup_checks=[],
    )
    app.state.metrics = metrics
    app.state.dependency_gauge = metrics.gauge(
        "dependency_up",
        "Degraded dependency liveness (1 up, 0 down).",
        labelnames=("dependency",),
    )
    app.state.rate_limit_exceeded = metrics.counter(
        "rate_limit_exceeded_total", "Total requests rejected by the rate limiter."
    )
    app.state.rate_limit_failopen = metrics.counter(
        "rate_limit_failopen_total", "Total requests that fell back to in-process rate limiting."
    )

    @app.exception_handler(RateLimitExceeded)
    async def _handle_rate_limit(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        response = error_response(request, 429, "rate_limited", "Rate limit exceeded")
        response.headers["Retry-After"] = str(exc.retry_after)
        return response

    app.include_router(router)
    return app


app = create_app()
