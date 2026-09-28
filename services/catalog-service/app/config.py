"""Service configuration for catalog-service."""

from __future__ import annotations

from functools import lru_cache

from kubecommerce_observability import BaseServiceSettings


class Settings(BaseServiceSettings):
    """Catalog-service settings loaded from the environment.

    Field names map to uppercase environment variables, so
    ``catalog_database_url`` is read from ``CATALOG_DATABASE_URL``.
    """

    service_name: str = "catalog-service"

    catalog_database_url: str
    catalog_redis_url: str
    catalog_cache_ttl_seconds: int = 60
    catalog_internal_api_token: str


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()
