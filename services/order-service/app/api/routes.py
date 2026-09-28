"""Public HTTP routes for order creation and retrieval."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_catalog,
    get_current_user_id,
    get_idempotency_key,
    get_session,
    require_internal_token,
)
from app.clients.catalog import CatalogClient
from app.models import Order
from app.schemas import (
    CreateOrderRequest,
    OrderItemResponse,
    OrderListResponse,
    OrderResponse,
)
from app.service import OrderService
from kubecommerce_observability import ApiError, get_correlation_id

router = APIRouter(tags=["orders"], dependencies=[Depends(require_internal_token)])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CatalogDep = Annotated[CatalogClient, Depends(get_catalog)]
UserDep = Annotated[UUID, Depends(get_current_user_id)]
IdempotencyDep = Annotated[str | None, Depends(get_idempotency_key)]


def _to_response(order: Order) -> OrderResponse:
    """Convert an ORM order into its API representation."""
    return OrderResponse(
        id=order.id,
        status=order.status,
        total_cents=order.total_cents,
        currency=order.currency,
        items=[
            OrderItemResponse(
                product_id=item.product_id,
                sku=item.sku,
                quantity=item.quantity,
                unit_price_cents=item.unit_price_cents,
            )
            for item in order.items
        ],
        created_at=order.created_at,
    )


def _service(request: Request, catalog: CatalogClient) -> OrderService:
    return OrderService(catalog, metrics=request.app.state.order_metrics)


@router.post("/orders", status_code=201, response_model=OrderResponse)
async def create_order(
    payload: CreateOrderRequest,
    request: Request,
    session: SessionDep,
    catalog: CatalogDep,
    user_id: UserDep,
    idempotency_key: IdempotencyDep,
) -> OrderResponse:
    """Create an order after validating products and reserving stock."""
    correlation_id = get_correlation_id() or uuid4().hex
    order = await _service(request, catalog).create_order(
        session,
        user_id=user_id,
        idempotency_key=idempotency_key,
        request=payload,
        correlation_id=correlation_id,
    )
    return _to_response(order)


@router.get("/orders", response_model=OrderListResponse)
async def list_orders(
    request: Request,
    session: SessionDep,
    catalog: CatalogDep,
    user_id: UserDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderListResponse:
    """List the caller's orders, newest first."""
    orders, total = await _service(request, catalog).list_orders(
        session, user_id=user_id, limit=limit, offset=offset
    )
    return OrderListResponse(
        items=[_to_response(order) for order in orders],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: UUID,
    request: Request,
    session: SessionDep,
    catalog: CatalogDep,
    user_id: UserDep,
) -> OrderResponse:
    """Return one of the caller's orders, or 404 when not owned."""
    order = await _service(request, catalog).get_order(session, order_id=order_id, user_id=user_id)
    if order is None:
        raise ApiError("order_not_found", "Order not found", status_code=404)
    return _to_response(order)
