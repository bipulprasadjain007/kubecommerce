"""Catalog-service Prometheus metrics, created once per app."""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import Counter

from kubecommerce_observability import ServiceMetrics


@dataclass(frozen=True)
class CatalogMetrics:
    """Bundle of catalog-specific metric instruments.

    Created once per application so the same ``Counter`` objects are shared by
    the cache and service layers (the Prometheus registry rejects duplicate
    registrations of a metric name).
    """

    cache_hits: Counter
    cache_misses: Counter
    cache_errors: Counter
    stock_conflicts: Counter
    db_errors: Counter

    @classmethod
    def create(cls, metrics: ServiceMetrics) -> CatalogMetrics:
        """Build the catalog metrics from the shared service metrics instance."""
        return cls(
            cache_hits=metrics.counter("cache_hits_total", "Product cache hits."),
            cache_misses=metrics.counter("cache_misses_total", "Product cache misses."),
            cache_errors=metrics.counter(
                "cache_errors_total",
                "Redis cache errors served fail-open.",
                ("operation",),
            ),
            stock_conflicts=metrics.counter(
                "stock_conflicts_total",
                "Rejected stock mutations caused by insufficient stock.",
                ("operation",),
            ),
            db_errors=metrics.counter(
                "db_errors_total",
                "Database errors during catalog writes.",
                ("operation",),
            ),
        )
