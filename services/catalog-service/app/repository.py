"""Data access functions for catalog-service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def get_by_sku(session: AsyncSession, sku: str) -> Product | None:
    """Return the product with ``sku``, if any."""
    result = await session.execute(select(Product).where(Product.sku == sku))
    return result.scalar_one_or_none()


async def get_product(session: AsyncSession, product_id: uuid.UUID) -> Product | None:
    """Return the product with ``product_id``, if any."""
    result = await session.execute(select(Product).where(Product.id == product_id))
    return result.scalar_one_or_none()


async def product_exists(session: AsyncSession, product_id: uuid.UUID) -> bool:
    """Return whether a product with ``product_id`` exists."""
    result = await session.execute(select(Product.id).where(Product.id == product_id))
    return result.scalar_one_or_none() is not None


async def list_products(
    session: AsyncSession, limit: int, offset: int
) -> tuple[list[Product], int]:
    """Return one page of products and the total count."""
    total = (await session.execute(select(func.count()).select_from(Product))).scalar_one()
    rows = await session.execute(
        select(Product).order_by(Product.created_at, Product.id).limit(limit).offset(offset)
    )
    return list(rows.scalars().all()), int(total)


async def adjust_stock(session: AsyncSession, product_id: uuid.UUID, delta: int) -> int | None:
    """Atomically apply ``delta`` when the result stays non-negative.

    Returns the new stock, or ``None`` when no row matched (missing product or
    the guard ``stock + delta >= 0`` failed).
    """
    statement = (
        update(Product)
        .where(Product.id == product_id, Product.stock + delta >= 0)
        .values(stock=Product.stock + delta, updated_at=_utcnow())
        .returning(Product.stock)
        .execution_options(synchronize_session=False)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def reserve_stock(session: AsyncSession, product_id: uuid.UUID, quantity: int) -> int | None:
    """Atomically decrement stock when at least ``quantity`` is available.

    Returns the new stock, or ``None`` when the guard failed or the product is
    missing.
    """
    statement = (
        update(Product)
        .where(Product.id == product_id, Product.stock >= quantity)
        .values(stock=Product.stock - quantity, updated_at=_utcnow())
        .returning(Product.stock)
        .execution_options(synchronize_session=False)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def release_stock(session: AsyncSession, product_id: uuid.UUID, quantity: int) -> int | None:
    """Atomically increment stock for an existing product.

    Returns the new stock, or ``None`` when the product is missing.
    """
    statement = (
        update(Product)
        .where(Product.id == product_id)
        .values(stock=Product.stock + quantity, updated_at=_utcnow())
        .returning(Product.stock)
        .execution_options(synchronize_session=False)
    )
    return (await session.execute(statement)).scalar_one_or_none()
