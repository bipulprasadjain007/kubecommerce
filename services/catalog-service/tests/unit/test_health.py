"""Health and metrics endpoint tests."""

from __future__ import annotations

from httpx import AsyncClient


async def test_health_live(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "catalog-service"


async def test_health_ready_checks_database(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok"}


async def test_metrics_exposed(client: AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text
