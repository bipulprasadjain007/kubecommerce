"""Tests for the shared ``order.created`` event contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from kubecommerce_contracts import (
    HEADER_CORRELATION_ID,
    HEADER_EVENT_ID,
    HEADER_EVENT_TYPE,
    HEADER_EVENT_VERSION,
    HEADER_RETRY_COUNT,
    HEADER_TRACEPARENT,
    OrderCreatedData,
    OrderCreatedEvent,
    OrderItem,
    UnsupportedEventVersionError,
    event_headers,
    new_order_created_event,
    parse_event,
    parse_order_created_event,
)

_CREATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _make_item(
    *,
    sku: str = "SKU-001",
    quantity: int = 2,
    unit_price_cents: int = 500,
) -> OrderItem:
    """Build a valid ``OrderItem`` instance for tests."""
    return OrderItem(
        product_id=uuid4(),
        sku=sku,
        quantity=quantity,
        unit_price_cents=unit_price_cents,
    )


def _make_data(
    created_at: datetime | None = None,
    *,
    items: list[OrderItem] | None = None,
    total_cents: int = 1000,
    currency: str = "USD",
) -> OrderCreatedData:
    """Build a valid ``OrderCreatedData`` instance for tests."""
    return OrderCreatedData(
        order_id=uuid4(),
        user_id=uuid4(),
        items=items if items is not None else [_make_item()],
        total_cents=total_cents,
        currency=currency,
        created_at=created_at or _CREATED_AT,
    )


def _make_event() -> OrderCreatedEvent:
    """Build a valid ``OrderCreatedEvent`` instance for tests."""
    return new_order_created_event(
        _make_data(),
        correlation_id="corr-123",
        producer="order-service",
    )


def test_json_round_trip_equality() -> None:
    event = _make_event()

    raw = event.model_dump_json()

    assert isinstance(raw, str)
    assert "2026-01-02T03:04:05" in raw
    assert parse_order_created_event(raw) == event


def test_model_dump_is_json_safe_with_iso_datetimes() -> None:
    event = _make_event()

    decoded = json.loads(event.model_dump_json())

    assert isinstance(decoded["occurred_at"], str)
    assert isinstance(decoded["data"]["created_at"], str)
    assert decoded["event_version"] == 1
    assert decoded["event_type"] == "order.created"


def test_defaults_are_populated() -> None:
    event = _make_event()

    assert event.event_type == "order.created"
    assert event.event_version == 1
    assert isinstance(event.event_id, UUID)
    assert event.occurred_at.tzinfo == UTC


def test_created_at_rejects_timezone_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _make_data(created_at=datetime(2026, 1, 2, 3, 4, 5))


def test_occurred_at_rejects_timezone_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        OrderCreatedEvent(
            event_type="order.created",
            correlation_id="corr-123",
            producer="order-service",
            data=_make_data(),
            occurred_at=datetime(2026, 1, 2, 3, 4, 5),
        )


def test_created_at_is_normalised_to_utc() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    data = _make_data(created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=offset))

    assert data.created_at.tzinfo == UTC
    assert data.created_at == datetime(2026, 1, 1, 21, 34, 5, tzinfo=UTC)


def test_naive_occurred_at_in_raw_payload_rejected() -> None:
    payload = json.loads(_make_event().model_dump_json())
    payload["occurred_at"] = "2026-01-02T03:04:05"

    with pytest.raises(ValidationError, match="timezone-aware"):
        parse_order_created_event(payload)


def test_missing_required_field_is_rejected() -> None:
    payload = json.loads(_make_event().model_dump_json())
    del payload["correlation_id"]

    with pytest.raises(ValidationError):
        parse_order_created_event(payload)


def test_missing_nested_required_field_is_rejected() -> None:
    payload = json.loads(_make_event().model_dump_json())
    del payload["data"]["items"][0]["sku"]

    with pytest.raises(ValidationError):
        parse_order_created_event(payload)


def test_extra_top_level_field_is_rejected() -> None:
    payload = json.loads(_make_event().model_dump_json())
    payload["unexpected_field"] = "nope"

    with pytest.raises(ValidationError):
        parse_order_created_event(payload)


def test_extra_nested_field_is_rejected() -> None:
    payload = json.loads(_make_event().model_dump_json())
    payload["data"]["unexpected_field"] = "nope"

    with pytest.raises(ValidationError):
        parse_order_created_event(payload)


@pytest.mark.parametrize("kind", ["bytes", "str", "dict"])
def test_parse_order_created_event_accepts_supported_inputs(kind: str) -> None:
    event = _make_event()
    serialized = event.model_dump_json()

    if kind == "bytes":
        raw: bytes | str | dict[str, object] = serialized.encode("utf-8")
    elif kind == "str":
        raw = serialized
    else:
        raw = json.loads(serialized)

    assert parse_order_created_event(raw) == event


def test_items_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        OrderCreatedData(
            order_id=uuid4(),
            user_id=uuid4(),
            items=[],
            total_cents=0,
            created_at=_CREATED_AT,
        )


def test_quantity_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        OrderItem(product_id=uuid4(), sku="SKU-001", quantity=0, unit_price_cents=100)


def test_unit_price_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        OrderItem(product_id=uuid4(), sku="SKU-001", quantity=1, unit_price_cents=-1)


def test_correlation_id_length_is_bounded() -> None:
    with pytest.raises(ValidationError):
        new_order_created_event(_make_data(), correlation_id="", producer="order-service")

    with pytest.raises(ValidationError):
        new_order_created_event(
            _make_data(),
            correlation_id="x" * 129,
            producer="order-service",
        )


# --------------------------------------------------------------------------- #
# Bounds and validation
# --------------------------------------------------------------------------- #


def test_producer_length_is_bounded() -> None:
    with pytest.raises(ValidationError):
        OrderCreatedEvent(
            event_type="order.created",
            correlation_id="corr-123",
            producer="p" * 129,
            data=_make_data(),
        )

    assert _make_event().producer == "order-service"


def test_sku_length_is_bounded() -> None:
    with pytest.raises(ValidationError):
        _make_item(sku="S" * 65)


def test_currency_must_be_exactly_three_characters() -> None:
    for currency in ("US", "USDD", ""):
        with pytest.raises(ValidationError):
            _make_data(currency=currency)


def test_items_are_bounded_to_one_hundred() -> None:
    items = [_make_item() for _ in range(101)]

    with pytest.raises(ValidationError):
        _make_data(items=items)


@pytest.mark.parametrize(
    "correlation_id",
    [
        "has space",
        "has$dollar",
        "has/slash",
        "has@at",
        "has:colon",
        "has\nnewline",
    ],
)
def test_correlation_id_rejects_invalid_characters(correlation_id: str) -> None:
    with pytest.raises(ValidationError):
        new_order_created_event(
            _make_data(),
            correlation_id=correlation_id,
            producer="order-service",
        )


def test_correlation_id_accepts_allowed_characters() -> None:
    event = new_order_created_event(
        _make_data(),
        correlation_id="Abc-123_x.y",
        producer="order-service",
    )

    assert event.correlation_id == "Abc-123_x.y"


def test_total_cents_is_authoritative_and_not_cross_checked() -> None:
    # Discounts/shipping make total_cents differ from the sum of line items.
    data = _make_data(items=[_make_item(quantity=2, unit_price_cents=500)], total_cents=1)

    assert data.total_cents == 1


# --------------------------------------------------------------------------- #
# AMQP headers
# --------------------------------------------------------------------------- #


def test_event_headers_content_and_types() -> None:
    event = _make_event()

    headers = event_headers(event)

    assert headers == {
        HEADER_EVENT_ID: str(event.event_id),
        HEADER_EVENT_TYPE: "order.created",
        HEADER_EVENT_VERSION: "1",
        HEADER_CORRELATION_ID: "corr-123",
        HEADER_RETRY_COUNT: "0",
    }
    assert all(isinstance(value, str) for value in headers.values())


def test_event_headers_include_retry_count_and_traceparent() -> None:
    headers = event_headers(_make_event(), retry_count=2, traceparent="00-trace-span-01")

    assert headers[HEADER_RETRY_COUNT] == "2"
    assert headers[HEADER_TRACEPARENT] == "00-trace-span-01"


def test_event_headers_omit_traceparent_when_not_provided() -> None:
    assert HEADER_TRACEPARENT not in event_headers(_make_event())


def test_event_headers_reject_negative_retry_count() -> None:
    with pytest.raises(ValueError, match="retry_count"):
        event_headers(_make_event(), retry_count=-1)


# --------------------------------------------------------------------------- #
# Schema evolution / version dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", ["bytes", "str", "dict"])
def test_parse_event_dispatches_v1(kind: str) -> None:
    event = _make_event()
    serialized = event.model_dump_json()

    if kind == "bytes":
        raw: bytes | str | dict[str, object] = serialized.encode("utf-8")
    elif kind == "str":
        raw = serialized
    else:
        raw = json.loads(serialized)

    assert parse_event(raw) == event


def test_parse_event_rejects_missing_version() -> None:
    payload = json.loads(_make_event().model_dump_json())
    del payload["event_version"]

    with pytest.raises(UnsupportedEventVersionError, match="event_version"):
        parse_event(payload)


def test_parse_event_rejects_unknown_version() -> None:
    payload = json.loads(_make_event().model_dump_json())
    payload["event_version"] = 2

    with pytest.raises(UnsupportedEventVersionError, match="unsupported"):
        parse_event(payload)


def test_parse_event_rejects_non_object_payload() -> None:
    with pytest.raises(UnsupportedEventVersionError, match="JSON object"):
        parse_event("[]")


def test_parse_order_created_event_delegates_to_parse_event() -> None:
    payload = json.loads(_make_event().model_dump_json())
    payload["event_version"] = 99

    with pytest.raises(UnsupportedEventVersionError):
        parse_order_created_event(payload)
