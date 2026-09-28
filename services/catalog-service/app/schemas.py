"""Pydantic request/response schemas for catalog-service."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProductCreate(BaseModel):
    """Payload for creating a product."""

    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    price_cents: int = Field(ge=0)
    stock: int = Field(ge=0)


class ProductRead(BaseModel):
    """A product as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sku: str
    name: str
    description: str | None
    price_cents: int
    stock: int
    created_at: datetime
    updated_at: datetime


class ProductList(BaseModel):
    """A page of products."""

    items: list[ProductRead]
    total: int
    limit: int
    offset: int


class StockPatch(BaseModel):
    """Payload for a relative stock adjustment."""

    delta: int

    @field_validator("delta")
    @classmethod
    def _delta_nonzero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("delta must not be zero")
        return value


class ReserveRequest(BaseModel):
    """Payload for an atomic stock reservation."""

    product_id: uuid.UUID
    quantity: int = Field(gt=0)


class ReleaseRequest(BaseModel):
    """Payload for a stock release (compensation)."""

    product_id: uuid.UUID
    quantity: int = Field(gt=0)


class ReserveResponse(BaseModel):
    """Result of a successful reservation."""

    product_id: uuid.UUID
    reserved: int
    stock: int


class ReleaseResponse(BaseModel):
    """Result of a successful release."""

    product_id: uuid.UUID
    released: int
    stock: int
