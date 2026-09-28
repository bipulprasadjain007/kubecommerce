"""Prometheus metrics helpers and middleware.

Assumes a single uvicorn worker per pod: the registry is per-process, so
horizontal scaling is done with replicas rather than multiple workers in one
process (which would split/duplicate metric series).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

DEFAULT_DURATION_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


class ServiceMetrics:
    """Container for a service's Prometheus metrics on a private registry.

    The registry is process-local; run one uvicorn worker per pod and scale with
    replicas so each process exposes its own consistent set of series.
    """

    def __init__(self, service_name: str, registry: CollectorRegistry | None = None) -> None:
        self.service_name = service_name
        self.registry = registry if registry is not None else CollectorRegistry()
        self._namespace = f"kubecommerce_{service_name}"

        self.requests_total = Counter(
            "http_requests_total",
            "Total number of HTTP requests.",
            labelnames=("service", "method", "route", "status"),
            namespace="kubecommerce",
            registry=self.registry,
        )
        self.request_duration_seconds = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds.",
            labelnames=("service", "method", "route"),
            namespace="kubecommerce",
            buckets=DEFAULT_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.requests_in_flight = Gauge(
            "http_requests_in_flight",
            "Number of in-flight HTTP requests.",
            labelnames=("service",),
            namespace="kubecommerce",
            registry=self.registry,
        )

    def counter(self, name: str, description: str, labelnames: Iterable[str] = ()) -> Counter:
        """Register and return a service-scoped counter on the shared registry."""
        return Counter(
            name,
            description,
            labelnames=tuple(labelnames),
            namespace=self._namespace,
            registry=self.registry,
        )

    def gauge(self, name: str, description: str, labelnames: Iterable[str] = ()) -> Gauge:
        """Register and return a service-scoped gauge on the shared registry."""
        return Gauge(
            name,
            description,
            labelnames=tuple(labelnames),
            namespace=self._namespace,
            registry=self.registry,
        )

    def histogram(
        self,
        name: str,
        description: str,
        labelnames: Iterable[str] = (),
        buckets: Sequence[float] | None = None,
    ) -> Histogram:
        """Register and return a service-scoped histogram on the shared registry."""
        kwargs: dict[str, Any] = {}
        if buckets is not None:
            kwargs["buckets"] = tuple(buckets)
        return Histogram(
            name,
            description,
            labelnames=tuple(labelnames),
            namespace=self._namespace,
            registry=self.registry,
            **kwargs,
        )

    def render(self) -> tuple[bytes, str]:
        """Return the current metrics exposition payload and its content type."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


def _route_path(request: Request) -> str:
    """Return the matched route template, or ``unmatched`` when none matched."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Record RED metrics for every HTTP request using 500 on unhandled errors."""

    def __init__(self, app: ASGIApp, metrics: ServiceMetrics) -> None:
        super().__init__(app)
        self.metrics = metrics

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        metrics = self.metrics
        service = metrics.service_name
        method = request.method
        status_code = 500
        metrics.requests_in_flight.labels(service=service).inc()
        started = time.perf_counter()
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            route_path = _route_path(request)
            elapsed = time.perf_counter() - started
            metrics.requests_total.labels(
                service=service,
                method=method,
                route=route_path,
                status=str(status_code),
            ).inc()
            metrics.request_duration_seconds.labels(
                service=service,
                method=method,
                route=route_path,
            ).observe(elapsed)
            metrics.requests_in_flight.labels(service=service).dec()


def metrics_endpoint(metrics: ServiceMetrics) -> Callable[[], Awaitable[Response]]:
    """Return an async endpoint that renders the service's metrics."""

    async def endpoint() -> Response:
        payload, content_type = metrics.render()
        return Response(content=payload, media_type=content_type)

    return endpoint
