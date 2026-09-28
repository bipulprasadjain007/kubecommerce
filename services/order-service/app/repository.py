"""Data-access helpers for orders and outbox events."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderItem, OutboxEvent


async def get_order_by_idempotency_key(session: AsyncSession, idempotency_key: str) -> Order | None:
    """Return the order stored for ``idempotency_key`` if any."""
    result = await session.execute(select(Order).where(Order.idempotency_key == idempotency_key))
    return result.scalar_one_or_none()


async def get_order_for_user(session: AsyncSession, order_id: UUID, user_id: UUID) -> Order | None:
    """Return ``order_id`` only when it belongs to ``user_id``."""
    result = await session.execute(
        select(Order).where(Order.id == order_id, Order.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def list_orders_for_user(
    session: AsyncSession, user_id: UUID, *, limit: int, offset: int
) -> list[Order]:
    """Return a page of ``user_id``'s orders, newest first."""
    result = await session.execute(
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def count_orders_for_user(session: AsyncSession, user_id: UUID) -> int:
    """Return the total number of orders owned by ``user_id``."""
    result = await session.execute(
        select(func.count()).select_from(Order).where(Order.user_id == user_id)
    )
    return int(result.scalar_one())


async def add_order(
    session: AsyncSession,
    order: Order,
    items: Sequence[OrderItem],
    outbox_event: OutboxEvent,
) -> None:
    """Persist the order, its items and one outbox event in a single commit."""
    order.items = list(items)
    session.add(order)
    session.add(outbox_event)
    await session.commit()
