"""Shared fixtures for notification-worker tests (no external services)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID

# Required settings must exist before importing app.main, whose module-level
# `app = create_app()` reads the environment at import time.
os.environ.setdefault("WORKER_RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
os.environ.setdefault("WORKER_REDIS_URL", "redis://localhost:6379/1")

import fakeredis.aioredis
import pytest

from app.config import Settings
from kubecommerce_contracts import (
    OrderCreatedData,
    OrderCreatedEvent,
    OrderItem,
    new_order_created_event,
)

ORDER_ID = UUID("0f8e7d6c-5b4a-4938-8271-6a5b4c3d2e1f")
USER_ID = UUID("1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9")
PRODUCT_ID = UUID("9c8b7a6d-5e4f-4132-8a79-b0c1d2e3f405")
CORRELATION_ID = "corr-abc-123"


def make_event() -> OrderCreatedEvent:
    """Build a fully populated, valid ``order.created`` event."""
    data = OrderCreatedData(
        order_id=ORDER_ID,
        user_id=USER_ID,
        items=[
            OrderItem(
                product_id=PRODUCT_ID,
                sku="SKU-0001",
                quantity=2,
                unit_price_cents=1999,
            )
        ],
        total_cents=3998,
        currency="USD",
        created_at=datetime.now(UTC),
    )
    return new_order_created_event(data, CORRELATION_ID, "order-service")


@pytest.fixture
def settings() -> Settings:
    """Return deterministic test settings."""
    return Settings(
        environment="dev",
        service_name="notification-worker",
        service_version="0.1.0",
        log_level="WARNING",
        worker_rabbitmq_url="amqp://guest:guest@localhost:5672/",
        worker_redis_url="redis://localhost:6379/1",
        worker_webhook_url="",
        worker_max_retries=3,
        worker_prefetch=10,
        worker_processing_ttl_seconds=60,
        worker_dedupe_ttl_seconds=604800,
        worker_webhook_timeout_seconds=5.0,
    )


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    """Return a fresh in-memory async Redis double."""
    return fakeredis.aioredis.FakeRedis()
