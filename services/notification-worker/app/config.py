"""Service configuration for notification-worker."""

from __future__ import annotations

from functools import lru_cache

from kubecommerce_observability import BaseServiceSettings


class Settings(BaseServiceSettings):
    """Notification-worker settings loaded from the environment.

    Field names map to uppercase environment variables, so
    ``worker_rabbitmq_url`` is read from ``WORKER_RABBITMQ_URL``.
    """

    service_name: str = "notification-worker"

    worker_rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    worker_redis_url: str = "redis://localhost:6379/1"
    #: Empty means "log structured notifications only"; no webhook is called.
    worker_webhook_url: str = ""
    worker_max_retries: int = 3
    worker_prefetch: int = 10
    #: TTL of the in-flight ``processing`` NX claim; short so a crashed handler
    #: becomes eligible for reprocessing without a 7-day stall.
    worker_processing_ttl_seconds: int = 60
    #: TTL of the terminal ``done`` marker; long to suppress long-lived duplicates.
    worker_dedupe_ttl_seconds: int = 604800
    worker_webhook_timeout_seconds: float = 5.0


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()
