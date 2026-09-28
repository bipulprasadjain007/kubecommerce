"""Pydantic request and response schemas for the order API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_STRICT_CONFIG = ConfigDict(extra="forbid")


class OrderItemRequest(BaseModel):
    """A single requested line item."""

    model_config = _STRICT_CONFIG

    product_id: UUID
    quantity: int = Field(ge=1, le=1000)


class CreateOrderRequest(BaseModel):
    """Request body for ``POST /orders``."""

    model_config = _STRICT_CONFIG

    items: list[OrderItemRequest] = Field(min_length=1, max_length=50)


class OrderItemResponse(BaseModel):
    """A persisted order line item."""

    product_id: UUID
    sku: str
    quantity: int
    unit_price_cents: int


class OrderResponse(BaseModel):
    """The canonical representation of an order."""

    id: UUID
    status: str
    total_cents: int
    currency: str
    items: list[OrderItemResponse]
    created_at: datetime


class OrderListResponse(BaseModel):
    """A page of the caller's orders."""

    items: list[OrderResponse]
    total: int
    limit: int
    offset: int
