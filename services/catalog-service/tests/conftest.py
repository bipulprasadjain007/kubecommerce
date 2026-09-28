"""Shared fixtures for catalog-service tests (no external services)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

# Required settings must exist before importing app.main, whose module-level
# `app = create_app()` reads the environment at import time.
os.environ.setdefault("CATALOG_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("CATALOG_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("CATALOG_INTERNAL_API_TOKEN", "test-internal-token")

import fakeredis.aioredis
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.main import create_app
from app.models import Base

INTERNAL_TOKEN = "test-internal-token"
INTERNAL_HEADERS = {"X-Internal-Token": INTERNAL_TOKEN}


class BrokenRedis(fakeredis.aioredis.FakeRedis):
    """FakeRedis whose operations always fail, to exercise the fail-open path."""

    async def get(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("redis unavailable")

    async def set(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("redis unavailable")

    async def incr(self, *args: object, **kwargs: object) -> object:
        raise ConnectionError("redis unavailable")


@pytest.fixture
def settings(tmp_path: object) -> Settings:
    """Return test settings backed by a per-test SQLite file."""
    database_path = f"{tmp_path}/catalog.db"
    return Settings(
        environment="dev",
        service_name="catalog-service",
        service_version="0.1.0",
        log_level="WARNING",
        catalog_database_url=f"sqlite+aiosqlite:///{database_path}",
        catalog_redis_url="redis://localhost:6379/0",
        catalog_cache_ttl_seconds=60,
        catalog_internal_api_token=INTERNAL_TOKEN,
    )


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    """Return a fresh in-memory async Redis double."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _start_app(application: FastAPI) -> AsyncIterator[FastAPI]:
    """Run the app lifespan and create the SQLite schema."""
    async with LifespanManager(application):
        engine: AsyncEngine = application.state.engine
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield application


@pytest.fixture
async def app(settings: Settings, fake_redis: Redis) -> AsyncIterator[FastAPI]:
    """Return the app under test wired to fakeredis."""
    application = create_app(settings, redis_client=fake_redis)
    async for started in _start_app(application):
        yield started


@pytest.fixture
async def broken_app(settings: Settings) -> AsyncIterator[FastAPI]:
    """Return the app under test wired to a Redis double that always errors."""
    application = create_app(settings, redis_client=BrokenRedis(decode_responses=True))
    async for started in _start_app(application):
        yield started


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Return an httpx client bound to the app's ASGI transport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest.fixture
async def broken_client(broken_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Return an httpx client bound to the Redis-degraded app."""
    transport = ASGITransport(app=broken_app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest.fixture
def create_product(client: AsyncClient) -> object:
    """Return a helper that creates a product via the internal endpoint."""

    async def _create(**overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "sku": "SKU-1",
            "name": "Widget",
            "description": "A widget",
            "price_cents": 1000,
            "stock": 10,
        }
        body.update(overrides)
        response = await client.post("/products", json=body, headers=INTERNAL_HEADERS)
        assert response.status_code == 201, response.text
        return response.json()

    return _create
