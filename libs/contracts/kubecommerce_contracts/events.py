"""Versioned event contracts shared across KubeCommerce microservices.

This module defines the canonical ``order.created`` event envelope that is
produced by the order service and consumed by background workers such as the
notification worker. Every model uses a strict configuration
(``extra="forbid"``) so malformed or partially migrated payloads fail fast at
the service boundary.

Schema evolution
----------------
Each wire payload carries an ``event_version`` discriminator. Version ``1`` is
today's only supported schema and is intentionally strict
(``extra="forbid"``). Adding, removing, or changing any field requires bumping
``event_version`` and introducing a new model rather than loosening an existing
one; :func:`parse_event` dispatches on the version and consumers route unknown
or missing versions to the dead-letter queue (DLQ) instead of guessing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kubecommerce_contracts.topics import (
    HEADER_CORRELATION_ID,
    HEADER_EVENT_ID,
    HEADER_EVENT_TYPE,
    HEADER_EVENT_VERSION,
    HEADER_RETRY_COUNT,
    HEADER_TRACEPARENT,
)

__all__ = [
    "OrderCreatedData",
    "OrderCreatedEvent",
    "OrderItem",
    "UnsupportedEventVersionError",
    "event_headers",
    "new_order_created_event",
    "parse_event",
    "parse_order_created_event",
]

_STRICT_CONFIG = ConfigDict(extra="forbid")

_CORRELATION_ID_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"

_SUPPORTED_EVENT_VERSIONS: Final[frozenset[int]] = frozenset({1})


class UnsupportedEventVersionError(ValueError):
    """Raised when an event payload is missing or carries an unknown version."""


def _normalise_utc(value: datetime, field_name: str) -> datetime:
    """Validate that ``value`` is timezone-aware and convert it to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware; received a timezone-naive datetime")
    return value.astimezone(UTC)


class OrderItem(BaseModel):
    """A single line item within an order."""

    model_config = _STRICT_CONFIG

    product_id: UUID
    sku: str = Field(max_length=64)
    quantity: int = Field(ge=1)
    unit_price_cents: int = Field(ge=0)


class OrderCreatedData(BaseModel):
    """Payload describing a newly created order.

    ``total_cents`` is authoritative: it is the amount the customer was charged
    and may include discounts, shipping, or taxes. It is deliberately *not*
    cross-checked against ``sum(quantity * unit_price_cents)`` because those
    adjustments are legitimate.
    """

    model_config = _STRICT_CONFIG

    order_id: UUID
    user_id: UUID
    items: list[OrderItem] = Field(min_length=1, max_length=100)
    total_cents: int = Field(ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        """Reject timezone-naive values and normalise aware values to UTC."""
        return _normalise_utc(value, "created_at")


class OrderCreatedEvent(BaseModel):
    """Canonical envelope for the versioned ``order.created`` event."""

    model_config = _STRICT_CONFIG

    event_type: Literal["order.created"]
    event_version: Literal[1] = 1
    event_id: UUID = Field(default_factory=uuid4)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str = Field(min_length=1, max_length=128, pattern=_CORRELATION_ID_PATTERN)
    producer: str = Field(max_length=128)
    data: OrderCreatedData

    @field_validator("occurred_at")
    @classmethod
    def _validate_occurred_at(cls, value: datetime) -> datetime:
        """Reject timezone-naive values and normalise aware values to UTC."""
        return _normalise_utc(value, "occurred_at")


def new_order_created_event(
    data: OrderCreatedData,
    correlation_id: str,
    producer: str,
) -> OrderCreatedEvent:
    """Build a fully populated ``order.created`` event envelope."""
    return OrderCreatedEvent(
        event_type="order.created",
        correlation_id=correlation_id,
        producer=producer,
        data=data,
    )


def parse_event(raw: bytes | str | dict[str, Any]) -> OrderCreatedEvent:
    """Decode a raw event payload, dispatching on its ``event_version``.

    ``raw`` may be UTF-8 encoded JSON bytes, a JSON string, or an already
    decoded mapping. Version ``1`` is the only supported schema today.

    :raises UnsupportedEventVersionError: if ``event_version`` is missing or
        names a version this library does not understand.
    :raises pydantic.ValidationError: if the payload is otherwise malformed.
    """
    if isinstance(raw, bytes):
        payload: Any = json.loads(raw.decode("utf-8"))
    elif isinstance(raw, str):
        payload = json.loads(raw)
    else:
        payload = raw

    if not isinstance(payload, dict):
        raise UnsupportedEventVersionError(
            "event payload must be a JSON object carrying an 'event_version' field"
        )

    version = payload.get("event_version")
    if version is None:
        raise UnsupportedEventVersionError(
            "event payload is missing the required 'event_version' field"
        )
    if version not in _SUPPORTED_EVENT_VERSIONS:
        supported = ", ".join(str(item) for item in sorted(_SUPPORTED_EVENT_VERSIONS))
        raise UnsupportedEventVersionError(
            f"unsupported event_version {version!r}; supported versions: {supported}"
        )

    return OrderCreatedEvent.model_validate(payload)


def parse_order_created_event(raw: bytes | str | dict[str, Any]) -> OrderCreatedEvent:
    """Decode and validate a raw ``order.created`` payload.

    Retained for backwards compatibility; delegates to :func:`parse_event`.
    """
    return parse_event(raw)


def event_headers(
    event: OrderCreatedEvent,
    *,
    retry_count: int = 0,
    traceparent: str | None = None,
) -> dict[str, str]:
    """Build the AMQP headers describing ``event``.

    All values are rendered as strings for transport. ``traceparent`` is only
    included when explicitly provided.

    :raises ValueError: if ``retry_count`` is negative.
    """
    if retry_count < 0:
        raise ValueError(f"retry_count must be non-negative; received {retry_count}")

    headers: dict[str, str] = {
        HEADER_EVENT_ID: str(event.event_id),
        HEADER_EVENT_TYPE: event.event_type,
        HEADER_EVENT_VERSION: str(event.event_version),
        HEADER_CORRELATION_ID: event.correlation_id,
        HEADER_RETRY_COUNT: str(retry_count),
    }
    if traceparent is not None:
        headers[HEADER_TRACEPARENT] = traceparent
    return headers
