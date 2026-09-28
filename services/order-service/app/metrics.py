"""Service-specific Prometheus metric containers.

Metric objects are registered once per process from the app's
:class:`~kubecommerce_observability.ServiceMetrics` registry and passed to the
components that record them. Label values stay bounded (never ids or raw paths).
"""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import Counter, Gauge, Histogram

from kubecommerce_observability import ServiceMetrics

_CATALOG_DURATION_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)


@dataclass(frozen=True)
class OutboxMetrics:
    """Metrics recorded by the transactional-outbox poller."""

    pending_events: Gauge
    oldest_pending_seconds: Gauge
    published_total: Counter
    publish_failures_total: Counter


@dataclass(frozen=True)
class OrderMetrics:
    """Metrics recorded while serving orders."""

    stock_conflicts_total: Counter
    catalog_request_duration_seconds: Histogram


def build_outbox_metrics(metrics: ServiceMetrics) -> OutboxMetrics:
    """Register and return the outbox metric set."""
    return OutboxMetrics(
        pending_events=metrics.gauge(
            "outbox_pending_events",
            "Number of unpublished outbox events.",
        ),
        oldest_pending_seconds=metrics.gauge(
            "outbox_oldest_pending_seconds",
            "Age in seconds of the oldest unpublished outbox event.",
        ),
        published_total=metrics.counter(
            "outbox_published_total",
            "Total outbox events successfully published.",
        ),
        publish_failures_total=metrics.counter(
            "outbox_publish_failures_total",
            "Total outbox publish failures.",
        ),
    )


def build_order_metrics(metrics: ServiceMetrics) -> OrderMetrics:
    """Register and return the order-serving metric set."""
    return OrderMetrics(
        stock_conflicts_total=metrics.counter(
            "stock_conflicts_total",
            "Total stock reservation conflicts.",
        ),
        catalog_request_duration_seconds=metrics.histogram(
            "catalog_request_duration_seconds",
            "Duration of catalog-service requests in seconds.",
            labelnames=("operation",),
            buckets=_CATALOG_DURATION_BUCKETS,
        ),
    )
