"""Order business logic: catalog validation, reservations and the outbox write.

Flow for ``POST /orders`` (enforced in this order):

1. Idempotency replay returns the previously stored order.
2. Each requested product is fetched from catalog-service (idempotent, retried).
3. Stock is reserved sequentially; any failure releases what was already
   reserved and surfaces the matching error.
4. The order, its items and one ``order.created`` outbox event are inserted in a
   single database transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.clients.catalog import CatalogClient, InsufficientStockError, PricedItem
from app.metrics import OrderMetrics
from app.models import Order, OrderItem, OutboxEvent
from app.schemas import CreateOrderRequest
from kubecommerce_contracts import (
    OrderCreatedData,
    OrderCreatedEvent,
    new_order_created_event,
)
from kubecommerce_contracts import (
    OrderItem as ContractOrderItem,
)
from kubecommerce_observability import get_logger

logger = get_logger("kubecommerce.order")

PRODUCER = "order-service"


@dataclass(frozen=True)
class PricedOrder:
    """A fully priced request ready to be persisted."""

    order_id: UUID
    items: tuple[PricedItem, ...]
    total_cents: int
    currency: str


def _total_cents(items: tuple[PricedItem, ...]) -> int:
    return sum(item.quantity * item.unit_price_cents for item in items)


class OrderService:
    """Coordinates catalog validation with transactional order persistence."""

    def __init__(self, catalog: CatalogClient, *, metrics: OrderMetrics) -> None:
        self._catalog = catalog
        self._metrics = metrics

    async def create_order(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        idempotency_key: str | None,
        request: CreateOrderRequest,
        correlation_id: str,
    ) -> Order:
        """Create (or replay) an order for ``user_id``."""
        if idempotency_key is not None:
            existing = await repository.get_order_by_idempotency_key(session, idempotency_key)
            if existing is not None:
                return existing

        order_id = uuid4()
        priced = await self._price_items(request, order_id)

        reserved: list[PricedItem] = []
        for item in priced.items:
            await self._reserve(item, reserved)

        now = datetime.now(UTC)
        event = self._build_event(priced, user_id=user_id, correlation_id=correlation_id, now=now)
        order = Order(
            id=priced.order_id,
            user_id=user_id,
            status="created",
            total_cents=priced.total_cents,
            currency=priced.currency,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        items = [
            OrderItem(
                product_id=item.product_id,
                sku=item.sku,
                quantity=item.quantity,
                unit_price_cents=item.unit_price_cents,
            )
            for item in priced.items
        ]
        outbox_event = OutboxEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            payload=event.model_dump_json(),
            correlation_id=correlation_id,
            created_at=now,
            next_attempt_at=now,
        )

        try:
            await repository.add_order(session, order, items, outbox_event)
        except IntegrityError:
            if idempotency_key is None:
                await self._release_all(reserved)
                raise
            await session.rollback()
            await self._release_all(reserved)
            existing = await repository.get_order_by_idempotency_key(session, idempotency_key)
            if existing is not None:
                logger.info("order_idempotency_race_recovered", order_id=str(existing.id))
                return existing
            raise
        except Exception:
            await session.rollback()
            await self._release_all(reserved)
            logger.error("order_persist_failed", order_id=str(order_id))
            raise

        logger.info("order_created", order_id=str(order_id), user_id=str(user_id))
        return order

    async def get_order(
        self, session: AsyncSession, *, order_id: UUID, user_id: UUID
    ) -> Order | None:
        """Return ``order_id`` only when owned by ``user_id``."""
        return await repository.get_order_for_user(session, order_id, user_id)

    async def list_orders(
        self, session: AsyncSession, *, user_id: UUID, limit: int, offset: int
    ) -> tuple[list[Order], int]:
        """Return a page of ``user_id``'s orders and the total count."""
        orders = await repository.list_orders_for_user(session, user_id, limit=limit, offset=offset)
        total = await repository.count_orders_for_user(session, user_id)
        return orders, total

    async def _price_items(self, request: CreateOrderRequest, order_id: UUID) -> PricedOrder:
        """Fetch each product and build the priced order."""
        priced: list[PricedItem] = []
        for line in request.items:
            product = await self._catalog.fetch_product(line.product_id)
            priced.append(
                PricedItem(
                    product_id=product.id,
                    sku=product.sku,
                    quantity=line.quantity,
                    unit_price_cents=product.price_cents,
                )
            )
        items = tuple(priced)
        return PricedOrder(
            order_id=order_id,
            items=items,
            total_cents=_total_cents(items),
            currency="USD",
        )

    def _build_event(
        self,
        priced: PricedOrder,
        *,
        user_id: UUID,
        correlation_id: str,
        now: datetime,
    ) -> OrderCreatedEvent:
        data = OrderCreatedData(
            order_id=priced.order_id,
            user_id=user_id,
            items=[
                ContractOrderItem(
                    product_id=item.product_id,
                    sku=item.sku,
                    quantity=item.quantity,
                    unit_price_cents=item.unit_price_cents,
                )
                for item in priced.items
            ],
            total_cents=priced.total_cents,
            currency=priced.currency,
            created_at=now,
        )
        return new_order_created_event(data, correlation_id=correlation_id, producer=PRODUCER)

    async def _reserve(self, item: PricedItem, reserved: list[PricedItem]) -> None:
        """Reserve one item, compensating prior reservations on failure."""
        try:
            await self._catalog.reserve(item)
        except InsufficientStockError:
            self._metrics.stock_conflicts_total.inc()
            await self._release_all(reserved)
            raise
        except Exception:
            await self._release_all(reserved)
            raise
        reserved.append(item)

    async def _release_all(self, items: list[PricedItem]) -> None:
        """Best-effort release of every already-reserved item."""
        for item in items:
            try:
                await self._catalog.release(item)
            except Exception as exc:
                logger.warning(
                    "stock_release_failed",
                    product_id=str(item.product_id),
                    error_type=exc.__class__.__name__,
                )
