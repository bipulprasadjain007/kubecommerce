"""Tests for Prometheus metrics and middleware."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from httpx import AsyncClient
from prometheus_client import CollectorRegistry

from kubecommerce_observability.metrics import (
    PrometheusMiddleware,
    ServiceMetrics,
    metrics_endpoint,
)


def _build_app(metrics: ServiceMetrics) -> FastAPI:
    app = FastAPI()
    app.add_middleware(PrometheusMiddleware, metrics=metrics)
    app.add_api_route("/metrics", metrics_endpoint(metrics), include_in_schema=False)

    @app.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    return app


async def test_metrics_use_route_template_and_status(
    make_client: Callable[..., AsyncClient],
) -> None:
    metrics = ServiceMetrics("orders", registry=CollectorRegistry())
    async with make_client(_build_app(metrics)) as client:
        response = await client.get("/items/abc-123")
    assert response.status_code == 200

    text = metrics.render()[0].decode()
    assert "kubecommerce_http_requests_total" in text
    assert 'route="/items/{item_id}"' in text
    assert 'status="200"' in text
    assert 'method="GET"' in text
    assert 'service="orders"' in text
    assert 'route="/items/abc-123"' not in text


async def test_unhandled_exception_records_500(
    make_client: Callable[..., AsyncClient],
) -> None:
    metrics = ServiceMetrics("orders", registry=CollectorRegistry())
    async with make_client(_build_app(metrics)) as client:
        response = await client.get("/boom")
    assert response.status_code == 500

    text = metrics.render()[0].decode()
    assert 'route="/boom"' in text
    assert 'status="500"' in text


async def test_metrics_endpoint_renders_exposition(
    make_client: Callable[..., AsyncClient],
) -> None:
    metrics = ServiceMetrics("catalog", registry=CollectorRegistry())
    async with make_client(_build_app(metrics)) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "kubecommerce_http_requests_total" in response.text


def test_factory_metrics_are_namespaced() -> None:
    metrics = ServiceMetrics("order-service", registry=CollectorRegistry())
    metrics.counter("orders_created_total", "Orders created", labelnames=("kind",)).labels(
        kind="online"
    ).inc()
    metrics.gauge("queue_depth", "Queue depth").set(3)
    metrics.histogram("handler_seconds", "Handler time", buckets=(0.1, 1.0)).observe(0.5)

    text = metrics.render()[0].decode()
    assert "kubecommerce_order_service_orders_created_total" in text
    assert "kubecommerce_order_service_queue_depth" in text
    assert "kubecommerce_order_service_handler_seconds" in text


def test_service_metrics_own_registry_is_isolated() -> None:
    first = ServiceMetrics("svc-a")
    second = ServiceMetrics("svc-a")
    assert first.registry is not second.registry
    first.requests_total.labels(service="svc-a", method="GET", route="/", status="200").inc()
    assert b"kubecommerce_http_requests_total" in first.render()[0]
    # The second instance has an independent registry with no samples.
    assert "svc-a" not in second.render()[0].decode()
