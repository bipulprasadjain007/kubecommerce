"""Service settings loaded from the environment."""

from __future__ import annotations

from functools import lru_cache

from kubecommerce_observability import BaseServiceSettings


class Settings(BaseServiceSettings):
    """Order-service configuration.

    Field names map to uppercase environment variables, e.g.
    ``orders_database_url`` -> ``ORDERS_DATABASE_URL``.
    """

    service_name: str = "order-service"

    orders_database_url: str
    orders_rabbitmq_url: str
    orders_catalog_base_url: str
    orders_internal_api_token: str
    orders_request_timeout_seconds: float = 5.0
    orders_outbox_poll_interval_seconds: float = 2.0
    orders_outbox_batch_size: int = 20


@lru_cache
def get_settings() -> Settings:
    """Return the cached service settings."""
    return Settings()
