"""Tests for the shared event topology constants."""

from __future__ import annotations

import pytest

from kubecommerce_contracts import (
    EXCHANGE_EVENTS,
    HEADER_RETRY_COUNT,
    MAX_RETRIES,
    QUEUE_ORDER_CREATED_NOTIFICATIONS,
    QUEUE_ORDER_CREATED_NOTIFICATIONS_DLQ,
    QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY,
    RETRY_DELAYS_MS,
    ROUTING_ORDER_CREATED,
    ROUTING_ORDER_CREATED_DLQ,
    ROUTING_ORDER_CREATED_RETRY,
    retry_queue_arguments,
    retry_routing_key,
)

_RETRY_PREFIX = "kubecommerce.notifications.order-created.retry."
_RETRY_ROUTING_PREFIX = "order.created.retry."


def test_core_topology_names() -> None:
    assert EXCHANGE_EVENTS == "kubecommerce.events"
    assert ROUTING_ORDER_CREATED == "order.created"
    assert QUEUE_ORDER_CREATED_NOTIFICATIONS == "kubecommerce.notifications.order-created"
    assert QUEUE_ORDER_CREATED_NOTIFICATIONS_DLQ == "kubecommerce.notifications.order-created.dlq"
    assert HEADER_RETRY_COUNT == "x-retry-count"


def test_retry_queue_count_matches_retry_delay_count() -> None:
    assert len(QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY) == len(RETRY_DELAYS_MS)


def test_retry_delays_are_positive_and_increasing() -> None:
    assert all(delay > 0 for delay in RETRY_DELAYS_MS)
    assert list(RETRY_DELAYS_MS) == sorted(RETRY_DELAYS_MS)


def test_retry_queue_names_are_unique_and_ordered() -> None:
    names = QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY

    assert len(set(names)) == len(names)
    for index, name in enumerate(names, start=1):
        assert name.startswith(_RETRY_PREFIX)
        assert name == f"{_RETRY_PREFIX}{index}"


def test_retry_queues_are_distinct_from_primary_and_dlq() -> None:
    for name in QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY:
        assert name != QUEUE_ORDER_CREATED_NOTIFICATIONS
        assert name != QUEUE_ORDER_CREATED_NOTIFICATIONS_DLQ


def test_max_retries_matches_delays_and_routing_keys() -> None:
    assert len(RETRY_DELAYS_MS) == MAX_RETRIES
    assert len(ROUTING_ORDER_CREATED_RETRY) == len(RETRY_DELAYS_MS)
    assert len(ROUTING_ORDER_CREATED_RETRY) == len(QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY)


def test_dlq_routing_key_is_distinct_from_primary() -> None:
    assert ROUTING_ORDER_CREATED_DLQ == "order.created.dead"
    assert ROUTING_ORDER_CREATED_DLQ != ROUTING_ORDER_CREATED


def test_retry_routing_keys_align_one_to_one_with_retry_queues() -> None:
    assert len(ROUTING_ORDER_CREATED_RETRY) == len(QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY)
    for index, queue_name in enumerate(QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY):
        tier = queue_name.removeprefix(_RETRY_PREFIX)
        assert ROUTING_ORDER_CREATED_RETRY[index] == f"{_RETRY_ROUTING_PREFIX}{tier}"


def test_retry_routing_key_is_one_based_and_bounded() -> None:
    for attempt in range(1, MAX_RETRIES + 1):
        assert retry_routing_key(attempt) == f"{_RETRY_ROUTING_PREFIX}{attempt}"
        assert retry_routing_key(attempt) == ROUTING_ORDER_CREATED_RETRY[attempt - 1]


@pytest.mark.parametrize("attempt", [0, -1, MAX_RETRIES + 1, MAX_RETRIES + 99])
def test_retry_routing_key_rejects_out_of_range(attempt: int) -> None:
    with pytest.raises(ValueError):
        retry_routing_key(attempt)


def test_retry_queue_arguments_values() -> None:
    for index, delay in enumerate(RETRY_DELAYS_MS):
        assert retry_queue_arguments(index) == {
            "x-message-ttl": delay,
            "x-dead-letter-exchange": EXCHANGE_EVENTS,
            "x-dead-letter-routing-key": ROUTING_ORDER_CREATED,
        }


@pytest.mark.parametrize("index", [-1, MAX_RETRIES, MAX_RETRIES + 1])
def test_retry_queue_arguments_reject_out_of_range(index: int) -> None:
    with pytest.raises((IndexError, ValueError)):
        retry_queue_arguments(index)
