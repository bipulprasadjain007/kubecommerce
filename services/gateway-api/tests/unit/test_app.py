"""Application-level tests: rate limiting, upstream errors and metrics."""

from __future__ import annotations

from typing import Any

import httpx
from conftest import BrokenRedis, running_client


async def test_rate_limit_exceeded_returns_429_with_retry_after(
    make_app: Any, http_mock: Any
) -> None:
    app = make_app(gateway_rate_limit_per_minute=2)
    http_mock.get("http://catalog-service/products").mock(return_value=httpx.Response(200, json=[]))

    async with running_client(app) as client:
        first = await client.get("/api/catalog/products")
        second = await client.get("/api/catalog/products")
        third = await client.get("/api/catalog/products")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    error = third.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["message"] == "Rate limit exceeded"
    assert "correlation_id" in error
    assert int(third.headers["Retry-After"]) >= 1


async def test_readiness_ok_even_with_redis_down(
    make_app: Any, broken_redis: BrokenRedis, http_mock: Any
) -> None:
    app = make_app(redis_client=broken_redis)

    async with running_client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {}


async def test_redis_down_fails_open_and_counts(
    make_app: Any, broken_redis: BrokenRedis, http_mock: Any
) -> None:
    app = make_app(redis_client=broken_redis)
    http_mock.get("http://catalog-service/products").mock(return_value=httpx.Response(200, json=[]))

    async with running_client(app) as client:
        response = await client.get("/api/catalog/products")
        metrics = await client.get("/metrics")

    assert response.status_code == 200
    text = metrics.text
    assert "rate_limit_failopen_total" in text
    line = next(
        row
        for row in text.splitlines()
        if row.startswith("kubecommerce_gateway_api_rate_limit_failopen_total ")
    )
    assert float(line.rsplit(" ", 1)[1]) == 1.0


async def test_upstream_timeout_returns_504(client: httpx.AsyncClient, http_mock: Any) -> None:
    http_mock.get("http://catalog-service/products").mock(
        side_effect=httpx.ConnectTimeout("timeout")
    )

    response = await client.get("/api/catalog/products")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"


async def test_upstream_connection_error_returns_502(
    client: httpx.AsyncClient, http_mock: Any
) -> None:
    http_mock.get("http://catalog-service/products").mock(side_effect=httpx.ConnectError("refused"))

    response = await client.get("/api/catalog/products")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


async def test_correlation_id_is_propagated_to_downstream(
    client: httpx.AsyncClient, http_mock: Any
) -> None:
    route = http_mock.get("http://catalog-service/products").mock(
        return_value=httpx.Response(200, json=[])
    )

    response = await client.get(
        "/api/catalog/products", headers={"X-Correlation-ID": "corr-abc-123"}
    )

    assert response.headers["X-Correlation-ID"] == "corr-abc-123"
    assert route.calls.last.request.headers["X-Correlation-ID"] == "corr-abc-123"


async def test_metrics_endpoint_exposes_custom_counters(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "rate_limit_exceeded_total" in response.text
    assert "rate_limit_failopen_total" in response.text
