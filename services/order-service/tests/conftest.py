"""Pytest fixtures for the order-service suite (no external services)."""

from __future__ import annotations

import os

# Environment must be populated before ``app.main`` is imported, because that
# module builds a module-level ``app = create_app()``.
os.environ.setdefault("ORDERS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ORDERS_RABBITMQ_URL", "amqp://guest:guest@localhost/")
os.environ.setdefault("ORDERS_CATALOG_BASE_URL", "http://catalog.test")
os.environ.setdefault("ORDERS_INTERNAL_API_TOKEN", "test-token")

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app
from app.models import Base
from tests.support import CATALOG_BASE_URL, FakePublisher


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Test settings backed by a temporary SQLite file."""
    return Settings(
        orders_database_url=f"sqlite+aiosqlite:///{tmp_path / 'orders.db'}",
        orders_rabbitmq_url="amqp://guest:guest@localhost/",
        orders_catalog_base_url=CATALOG_BASE_URL,
        orders_internal_api_token="test-token",
        orders_request_timeout_seconds=1.0,
        orders_outbox_poll_interval_seconds=3600.0,
        orders_outbox_batch_size=20,
    )


@pytest.fixture
def publisher() -> FakePublisher:
    """A fake broker publisher injected into the outbox poller."""
    return FakePublisher()


@pytest.fixture
async def app(settings: Settings, publisher: FakePublisher) -> AsyncIterator[FastAPI]:
    """A lifespan-managed app with schema created and a fake publisher."""
    application = create_app(settings, publish=publisher.publish)
    async with LifespanManager(application):
        async with application.state.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client that talks to the ASGI app in-process."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://orders.test") as http_client:
        yield http_client


@pytest.fixture
def catalog_mock() -> Iterator[respx.Router]:
    """A respx router that intercepts catalog-service HTTP calls."""
    with respx.mock(assert_all_called=False) as router:
        yield router
