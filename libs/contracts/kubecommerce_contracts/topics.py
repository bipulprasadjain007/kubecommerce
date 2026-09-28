"""Names and routing constants for the KubeCommerce event infrastructure.

Topology
--------
The ``order.created`` flow shares a single durable topic exchange
(:data:`EXCHANGE_EVENTS`) fronting one primary queue plus a tiered retry chain
and a dead-letter queue:

* The primary queue (:data:`QUEUE_ORDER_CREATED_NOTIFICATIONS`) is bound to the
  exchange with routing key ``order.created``.
* On handler failure the worker republishes the message to the exchange with
  routing key ``order.created.retry.N``. Retry queue ``N`` holds the message for
  ``RETRY_DELAYS_MS[N - 1]`` milliseconds before dead-lettering it back to the
  exchange with routing key ``order.created``, which returns it to the primary
  queue.
* After :data:`MAX_RETRIES` failed attempts the worker republishes the message
  with routing key ``order.created.dead``, landing it on the DLQ
  (:data:`QUEUE_ORDER_CREATED_NOTIFICATIONS_DLQ`).
* Poison messages and validation failures skip retries and go straight to the
  DLQ.

The retry queues, :data:`ROUTING_ORDER_CREATED_RETRY`, and
:data:`RETRY_DELAYS_MS` are aligned index-by-index: entry ``i`` of each tuple
describes the same retry tier.
"""

from typing import Any, Final

__all__ = [
    "EXCHANGE_EVENTS",
    "HEADER_CORRELATION_ID",
    "HEADER_EVENT_ID",
    "HEADER_EVENT_TYPE",
    "HEADER_EVENT_VERSION",
    "HEADER_RETRY_COUNT",
    "HEADER_TRACEPARENT",
    "MAX_RETRIES",
    "QUEUE_ORDER_CREATED_NOTIFICATIONS",
    "QUEUE_ORDER_CREATED_NOTIFICATIONS_DLQ",
    "QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY",
    "RETRY_DELAYS_MS",
    "ROUTING_ORDER_CREATED",
    "ROUTING_ORDER_CREATED_DLQ",
    "ROUTING_ORDER_CREATED_RETRY",
    "retry_queue_arguments",
    "retry_routing_key",
]

EXCHANGE_EVENTS: Final[str] = "kubecommerce.events"
"""Durable topic exchange that carries all KubeCommerce domain events."""

ROUTING_ORDER_CREATED: Final[str] = "order.created"
"""Routing key used to publish ``order.created`` events to the primary queue."""

QUEUE_ORDER_CREATED_NOTIFICATIONS: Final[str] = "kubecommerce.notifications.order-created"
"""Primary durable queue consumed by the notification worker."""

QUEUE_ORDER_CREATED_NOTIFICATIONS_RETRY: Final[tuple[str, ...]] = (
    "kubecommerce.notifications.order-created.retry.1",
    "kubecommerce.notifications.order-created.retry.2",
    "kubecommerce.notifications.order-created.retry.3",
)
"""Ordered tiered retry queues; index ``i`` pairs with ``RETRY_DELAYS_MS[i]``."""

QUEUE_ORDER_CREATED_NOTIFICATIONS_DLQ: Final[str] = "kubecommerce.notifications.order-created.dlq"
"""Dead-letter queue for messages that exhaust all retry attempts."""

RETRY_DELAYS_MS: Final[tuple[int, ...]] = (5000, 30000, 120000)
"""Per-tier retry delays in milliseconds, aligned with the retry queues."""

MAX_RETRIES: Final[int] = len(RETRY_DELAYS_MS)
"""Total number of retry attempts available before a message is dead-lettered."""

ROUTING_ORDER_CREATED_RETRY: Final[tuple[str, ...]] = (
    "order.created.retry.1",
    "order.created.retry.2",
    "order.created.retry.3",
)
"""Retry routing keys aligned 1:1 (by index) with the retry queues."""

ROUTING_ORDER_CREATED_DLQ: Final[str] = "order.created.dead"
"""Routing key used to publish messages that have exhausted all retries."""

HEADER_RETRY_COUNT: Final[str] = "x-retry-count"
"""Message header tracking how many delivery attempts have already occurred."""

HEADER_EVENT_ID: Final[str] = "event_id"
"""AMQP header carrying the ``event_id`` of the enveloped event."""

HEADER_EVENT_TYPE: Final[str] = "event_type"
"""AMQP header carrying the event type discriminator."""

HEADER_EVENT_VERSION: Final[str] = "event_version"
"""AMQP header carrying the schema version of the enveloped event."""

HEADER_CORRELATION_ID: Final[str] = "X-Correlation-ID"
"""AMQP header carrying the request correlation identifier."""

HEADER_TRACEPARENT: Final[str] = "traceparent"
"""AMQP header carrying the W3C trace context (``traceparent``)."""


def retry_routing_key(attempt: int) -> str:
    """Return the routing key for a 1-based retry ``attempt``.

    :raises ValueError: if ``attempt`` is not within ``1..MAX_RETRIES``.
    """
    if not 1 <= attempt <= MAX_RETRIES:
        raise ValueError(f"attempt must be between 1 and {MAX_RETRIES}; received {attempt}")
    return ROUTING_ORDER_CREATED_RETRY[attempt - 1]


def retry_queue_arguments(index: int) -> dict[str, Any]:
    """Return the RabbitMQ queue arguments for the 0-based retry tier ``index``.

    The queue expires messages after its configured TTL and dead-letters them
    back to the events exchange using the primary ``order.created`` routing key.

    :raises IndexError: if ``index`` is not within ``0..MAX_RETRIES - 1``.
    """
    if not 0 <= index < MAX_RETRIES:
        raise IndexError(f"index must be between 0 and {MAX_RETRIES - 1}; received {index}")
    return {
        "x-message-ttl": RETRY_DELAYS_MS[index],
        "x-dead-letter-exchange": EXCHANGE_EVENTS,
        "x-dead-letter-routing-key": ROUTING_ORDER_CREATED,
    }
