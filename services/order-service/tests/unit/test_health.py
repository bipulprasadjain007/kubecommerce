"""Tests for health, readiness and engine helpers."""

from __future__ import annotations

from pathlib import Path

import httpx
from asgi_lifespan import LifespanManager

from app.config import Settings
from app.db import check_database, create_engine
from app.main import create_app


async def test_liveness_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_checks_database_only(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok"}


async def test_check_database_returns_false_when_connection_fails(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    engine = create_engine(f"sqlite+aiosqlite:///{blocker}/orders.db")

    try:
        assert await check_database(engine) is False
    finally:
        await engine.dispose()


async def test_create_engine_supports_postgres_urls() -> None:
    engine = create_engine("postgresql+asyncpg://user:pass@localhost:5432/orders_db")

    assert engine.sync_engine.dialect.name == "postgresql"
    await engine.dispose()


async def test_app_starts_without_broker(settings: Settings) -> None:
    """A real (lazy) publisher is wired but no broker is contacted at startup."""
    application = create_app(settings)

    async with LifespanManager(application):
        assert application.state.publisher is not None
        assert application.state.outbox_task is not None


async def test_metrics_endpoint_is_exposed(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert b"kubecommerce_order_service_outbox_pending_events" in response.content


async def test_unmatched_route_uses_standard_error(client: httpx.AsyncClient) -> None:
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_error"
