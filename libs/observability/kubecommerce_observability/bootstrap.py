"""One-call observability bootstrap for KubeCommerce FastAPI services."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import FastAPI

from .errors import register_exception_handlers
from .health import ReadinessCheck, build_health_router
from .logging import configure_logging
from .metrics import PrometheusMiddleware, ServiceMetrics, metrics_endpoint
from .middleware import AccessLogMiddleware, CorrelationIdMiddleware
from .settings import BaseServiceSettings
from .tracing import configure_tracing, instrument_fastapi


def install_observability(
    app: FastAPI,
    *,
    settings: BaseServiceSettings,
    readiness_checks: Sequence[ReadinessCheck] = (),
    startup_checks: Sequence[ReadinessCheck] = (),
) -> ServiceMetrics:
    """Install logging, metrics, tracing, middleware, health and error handling.

    Assumes a single uvicorn worker per pod: the Prometheus registry is
    per-process, so scale horizontally with replicas rather than multiple
    workers sharing one process.

    Returns the :class:`ServiceMetrics` instance registered on the app.
    """
    configure_logging(settings.service_name, settings.environment, settings.log_level)

    metrics = ServiceMetrics(settings.service_name)

    # Starlette prepends middleware, so add the innermost first. The resulting
    # order is CorrelationId (outer) -> AccessLog -> Prometheus (inner).
    app.add_middleware(PrometheusMiddleware, metrics=metrics)
    app.add_middleware(AccessLogMiddleware, service_name=settings.service_name)
    app.add_middleware(CorrelationIdMiddleware)

    register_exception_handlers(app)

    app.include_router(
        build_health_router(
            settings.service_name,
            settings.service_version,
            readiness_checks,
            startup_checks=startup_checks,
        )
    )
    app.add_api_route("/metrics", metrics_endpoint(metrics), include_in_schema=False)

    if settings.otel_enabled:
        configure_tracing(
            settings.service_name,
            settings.environment,
            service_version=settings.service_version,
            otlp_endpoint=settings.otel_exporter_otlp_endpoint,
            enabled=True,
            sample_ratio=settings.otel_sample_ratio,
        )
        instrument_fastapi(app, settings.service_name)

    return metrics
