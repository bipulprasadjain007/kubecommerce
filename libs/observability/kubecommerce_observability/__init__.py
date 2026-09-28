"""Shared observability library for KubeCommerce FastAPI microservices."""

from __future__ import annotations

from .bootstrap import install_observability
from .context import (
    correlation_headers,
    get_correlation_id,
    get_trace_id,
    reset_context,
    set_correlation_id,
    set_trace_id,
)
from .errors import ApiError, error_response, register_exception_handlers
from .health import ReadinessCheck, build_health_router
from .http import create_http_client
from .logging import configure_logging, get_logger
from .metrics import PrometheusMiddleware, ServiceMetrics, metrics_endpoint
from .middleware import AccessLogMiddleware, CorrelationIdMiddleware
from .settings import BaseServiceSettings
from .tracing import (
    configure_tracing,
    get_tracer,
    instrument_fastapi,
    shutdown_tracing,
)

__all__ = [
    "AccessLogMiddleware",
    "ApiError",
    "BaseServiceSettings",
    "CorrelationIdMiddleware",
    "PrometheusMiddleware",
    "ReadinessCheck",
    "ServiceMetrics",
    "build_health_router",
    "configure_logging",
    "configure_tracing",
    "correlation_headers",
    "create_http_client",
    "error_response",
    "get_correlation_id",
    "get_logger",
    "get_trace_id",
    "get_tracer",
    "install_observability",
    "instrument_fastapi",
    "metrics_endpoint",
    "register_exception_handlers",
    "reset_context",
    "set_correlation_id",
    "set_trace_id",
    "shutdown_tracing",
]
