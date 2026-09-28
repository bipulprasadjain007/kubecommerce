"""HTTP routes for catalog-service."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import service
from app.api.deps import get_cache, get_session, require_internal_token
from app.cache import CatalogCache
from app.metrics import CatalogMetrics
from app.schemas import (
    ProductCreate,
    ProductList,
    ProductRead,
    ReleaseRequest,
    ReleaseResponse,
    ReserveRequest,
    ReserveResponse,
    StockPatch,
)

router = APIRouter()

type SessionDep = Annotated[AsyncSession, Depends(get_session)]
type CacheDep = Annotated[CatalogCache, Depends(get_cache)]
InternalAuth = Depends(require_internal_token)


def _metrics(request: Request) -> CatalogMetrics:
    return request.app.state.catalog_metrics


@router.post(
    "/products",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[InternalAuth],
)
async def create_product(
    payload: ProductCreate, request: Request, session: SessionDep, cache: CacheDep
) -> ProductRead:
    """Create a product (internal mutation)."""
    return await service.create_product(session, cache, _metrics(request), payload)


@router.get("/products", response_model=ProductList)
async def list_products(
    session: SessionDep,
    cache: CacheDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProductList:
    """List products (public, cached)."""
    return await service.list_products(session, cache, limit, offset)


@router.get("/products/{product_id}", response_model=ProductRead)
async def get_product(product_id: uuid.UUID, session: SessionDep, cache: CacheDep) -> ProductRead:
    """Fetch a single product (public, cached)."""
    return await service.get_product(session, cache, product_id)


@router.patch(
    "/products/{product_id}/stock",
    response_model=ProductRead,
    dependencies=[InternalAuth],
)
async def patch_stock(
    product_id: uuid.UUID,
    payload: StockPatch,
    request: Request,
    session: SessionDep,
    cache: CacheDep,
) -> ProductRead:
    """Atomically adjust a product's stock (internal mutation)."""
    return await service.update_stock(session, cache, _metrics(request), product_id, payload.delta)


@router.post(
    "/internal/stock/reserve",
    response_model=ReserveResponse,
    dependencies=[InternalAuth],
)
async def reserve_stock(
    payload: ReserveRequest, request: Request, session: SessionDep, cache: CacheDep
) -> ReserveResponse:
    """Atomically reserve stock for an order (internal)."""
    return await service.reserve_stock(session, cache, _metrics(request), payload)


@router.post(
    "/internal/stock/release",
    response_model=ReleaseResponse,
    dependencies=[InternalAuth],
)
async def release_stock(
    payload: ReleaseRequest, request: Request, session: SessionDep, cache: CacheDep
) -> ReleaseResponse:
    """Compensate a reservation by restoring stock (internal)."""
    return await service.release_stock(session, cache, _metrics(request), payload)
