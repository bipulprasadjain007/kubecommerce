"""Business logic for catalog-service (cache-aside reads, atomic inventory)."""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository as repo
from app.cache import CatalogCache
from app.metrics import CatalogMetrics
from app.models import Product
from app.schemas import (
    ProductCreate,
    ProductList,
    ProductRead,
    ReleaseRequest,
    ReleaseResponse,
    ReserveRequest,
    ReserveResponse,
)
from kubecommerce_observability import ApiError


def _not_found() -> ApiError:
    return ApiError("product_not_found", "Product not found", status_code=404)


async def create_product(
    session: AsyncSession,
    cache: CatalogCache,
    metrics: CatalogMetrics,
    payload: ProductCreate,
) -> ProductRead:
    """Create a product and invalidate the cache after commit."""
    if await repo.get_by_sku(session, payload.sku) is not None:
        raise ApiError(
            "sku_already_exists", "A product with this SKU already exists", status_code=409
        )

    product = Product(
        sku=payload.sku,
        name=payload.name,
        description=payload.description,
        price_cents=payload.price_cents,
        stock=payload.stock,
    )
    session.add(product)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        metrics.db_errors.labels(operation="create_product").inc()
        raise ApiError(
            "sku_already_exists", "A product with this SKU already exists", status_code=409
        ) from exc

    await cache.bump_version()
    return ProductRead.model_validate(product)


async def get_product(
    session: AsyncSession, cache: CatalogCache, product_id: uuid.UUID
) -> ProductRead:
    """Return a product, serving from the cache when possible."""
    version = await cache.get_version()
    cached = await cache.get_item(version, product_id)
    if cached is not None:
        return cached

    product = await repo.get_product(session, product_id)
    if product is None:
        raise _not_found()

    result = ProductRead.model_validate(product)
    await cache.set_item(version, product_id, result)
    return result


async def list_products(
    session: AsyncSession, cache: CatalogCache, limit: int, offset: int
) -> ProductList:
    """Return one page of products, serving from the cache when possible."""
    version = await cache.get_version()
    cached = await cache.get_list(version, limit, offset)
    if cached is not None:
        return cached

    products, total = await repo.list_products(session, limit, offset)
    result = ProductList(
        items=[ProductRead.model_validate(product) for product in products],
        total=total,
        limit=limit,
        offset=offset,
    )
    await cache.set_list(version, limit, offset, result)
    return result


async def update_stock(
    session: AsyncSession,
    cache: CatalogCache,
    metrics: CatalogMetrics,
    product_id: uuid.UUID,
    delta: int,
) -> ProductRead:
    """Atomically adjust stock, then invalidate the cache."""
    new_stock = await repo.adjust_stock(session, product_id, delta)
    if new_stock is None:
        if not await repo.product_exists(session, product_id):
            raise _not_found()
        metrics.stock_conflicts.labels(operation="patch_stock").inc()
        raise ApiError("insufficient_stock", "Stock cannot become negative", status_code=409)

    await session.commit()
    product = await repo.get_product(session, product_id)
    if product is None:  # pragma: no cover - deleted between update and read
        raise _not_found()
    await cache.bump_version()
    return ProductRead.model_validate(product)


async def reserve_stock(
    session: AsyncSession,
    cache: CatalogCache,
    metrics: CatalogMetrics,
    payload: ReserveRequest,
) -> ReserveResponse:
    """Atomically reserve stock for an order."""
    new_stock = await repo.reserve_stock(session, payload.product_id, payload.quantity)
    if new_stock is None:
        if not await repo.product_exists(session, payload.product_id):
            raise _not_found()
        metrics.stock_conflicts.labels(operation="reserve_stock").inc()
        raise ApiError("insufficient_stock", "Not enough stock available", status_code=409)

    await session.commit()
    await cache.bump_version()
    return ReserveResponse(
        product_id=payload.product_id, reserved=payload.quantity, stock=new_stock
    )


async def release_stock(
    session: AsyncSession,
    cache: CatalogCache,
    metrics: CatalogMetrics,
    payload: ReleaseRequest,
) -> ReleaseResponse:
    """Compensate a reservation by restoring stock."""
    new_stock = await repo.release_stock(session, payload.product_id, payload.quantity)
    if new_stock is None:
        raise _not_found()

    await session.commit()
    await cache.bump_version()
    return ReleaseResponse(
        product_id=payload.product_id, released=payload.quantity, stock=new_stock
    )
