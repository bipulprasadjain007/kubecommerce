"""Shared test helpers: fakes, catalog mocking and database inspection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import respx
from fastapi import FastAPI
from sqlalchemy import select

from app.models import Order, OrderItem, OutboxEvent
from kubecommerce_contracts import (
    OrderCreatedData,
    OrderCreatedEvent,
    new_order_created_event,
)
from kubecommerce_contracts import (
    OrderItem as ContractOrderItem,
)

CATALOG_BASE_URL = "http://catalog.test"
PRODUCER = "order-service"


class FakePublisher:
    """Records published bodies/headers and can be told to fail."""

    def __init__(self) -> None:
        self.published: list[tuple[bytes, dict[str, str]]] = []
        self.error: BaseException | None = None

    async def publish(self, body: bytes, headers: dict[str, str]) -> None:
        if self.error is not None:
            raise self.error
        self.published.append((body, headers))


def auth_headers(user_id: UUID) -> dict[str, str]:
    """Return the gateway-authenticated headers for ``user_id``."""
    return {"X-User-ID": str(user_id), "X-Internal-Token": "test-token"}


def product_payload(
    product_id: UUID,
    *,
    sku: str = "SKU-001",
    price: int = 1999,
    stock: int = 100,
) -> dict[str, Any]:
    """Return a catalog-service ``ProductRead`` response body."""
    now = datetime.now(UTC).isoformat()
    return {
        "id": str(product_id),
        "sku": sku,
        "name": f"Product {sku}",
        "description": None,
        "price_cents": price,
        "stock": stock,
        "created_at": now,
        "updated_at": now,
    }


def mock_product(router: respx.Router, product_id: UUID, **kwargs: Any) -> respx.Route:
    """Mock ``GET /products/{id}`` to return a product."""
    return router.get(f"{CATALOG_BASE_URL}/products/{product_id}").mock(
        return_value=httpx.Response(200, json=product_payload(product_id, **kwargs))
    )


def mock_reserve(router: respx.Router, **mock_kwargs: Any) -> respx.Route:
    """Mock ``POST /internal/stock/reserve``."""
    return router.post(f"{CATALOG_BASE_URL}/internal/stock/reserve").mock(**mock_kwargs)


def mock_release(router: respx.Router, **mock_kwargs: Any) -> respx.Route:
    """Mock ``POST /internal/stock/release``."""
    return router.post(f"{CATALOG_BASE_URL}/internal/stock/release").mock(**mock_kwargs)


async def get_orders(app: FastAPI) -> list[Order]:
    """Return every persisted order."""
    async with app.state.session_factory() as session:
        result = await session.execute(select(Order))
        return list(result.scalars().all())


async def get_outbox(app: FastAPI) -> list[OutboxEvent]:
    """Return every persisted outbox event."""
    async with app.state.session_factory() as session:
        result = await session.execute(select(OutboxEvent))
        return list(result.scalars().all())


async def seed_order(
    app: FastAPI,
    user_id: UUID,
    *,
    created_at: datetime,
    idempotency_key: str | None = None,
) -> UUID:
    """Insert an order directly, bypassing the API."""
    async with app.state.session_factory() as session:
        order = Order(
            user_id=user_id,
            status="created",
            total_cents=100,
            currency="USD",
            idempotency_key=idempotency_key,
            created_at=created_at,
            updated_at=created_at,
        )
        order.items = [
            OrderItem(product_id=uuid4(), sku="SKU-SEED", quantity=1, unit_price_cents=100)
        ]
        session.add(order)
        await session.commit()
        return order.id


def build_event() -> OrderCreatedEvent:
    """Build a valid ``order.created`` event for outbox tests."""
    return new_order_created_event(
        OrderCreatedData(
            order_id=uuid4(),
            user_id=uuid4(),
            items=[
                ContractOrderItem(
                    product_id=uuid4(),
                    sku="SKU-OUTBOX",
                    quantity=1,
                    unit_price_cents=100,
                )
            ],
            total_cents=100,
            currency="USD",
            created_at=datetime.now(UTC),
        ),
        correlation_id="corr-outbox",
        producer=PRODUCER,
    )


async def seed_outbox(
    app: FastAPI,
    *,
    event: OrderCreatedEvent | None = None,
    attempts: int = 0,
    next_attempt_at: datetime | None = None,
    created_at: datetime | None = None,
) -> OrderCreatedEvent:
    """Insert a pending outbox row and return its event."""
    resolved = event if event is not None else build_event()
    now = datetime.now(UTC)
    async with app.state.session_factory() as session:
        session.add(
            OutboxEvent(
                event_id=resolved.event_id,
                event_type=resolved.event_type,
                payload=resolved.model_dump_json(),
                correlation_id=resolved.correlation_id,
                created_at=created_at if created_at is not None else now,
                next_attempt_at=next_attempt_at if next_attempt_at is not None else now,
                attempts=attempts,
            )
        )
        await session.commit()
    return resolved
