"""Gateway service configuration."""

from __future__ import annotations

from functools import lru_cache

from kubecommerce_observability import BaseServiceSettings


class Settings(BaseServiceSettings):
    """Settings for the public gateway.

    Field names map to uppercase environment variables, e.g.
    ``gateway_redis_url`` -> ``GATEWAY_REDIS_URL``.
    """

    service_name: str = "gateway-api"

    gateway_redis_url: str = "redis://localhost:6379/2"
    gateway_auth_base_url: str = "http://auth-service:8000"
    gateway_catalog_base_url: str = "http://catalog-service:8000"
    gateway_order_base_url: str = "http://order-service:8000"
    gateway_jwks_url: str = "http://auth-service:8000/.well-known/jwks.json"
    gateway_jwt_issuer: str = "kubecommerce-auth"
    gateway_jwt_audience: str = "kubecommerce"
    gateway_rate_limit_per_minute: int = 120
    gateway_request_timeout_seconds: float = 5.0
    gateway_internal_api_token: str
    gateway_jwks_cache_ttl_seconds: int = 300
    gateway_dependency_check_interval_seconds: int = 30


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loaded from the environment."""
    return Settings()
