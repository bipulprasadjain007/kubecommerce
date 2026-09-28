"""Service configuration for auth-service."""

from __future__ import annotations

from functools import lru_cache

from kubecommerce_observability import BaseServiceSettings


class Settings(BaseServiceSettings):
    """Auth-service settings loaded from the environment.

    Field names map to uppercase environment variables, so
    ``auth_database_url`` is read from ``AUTH_DATABASE_URL``. The signing key
    may be supplied either inline (``AUTH_JWT_PRIVATE_KEY``) or as a mounted PEM
    file (``AUTH_JWT_PRIVATE_KEY_FILE``); absence is detected at app creation.
    """

    service_name: str = "auth-service"

    auth_database_url: str
    auth_jwt_private_key: str | None = None
    auth_jwt_private_key_file: str | None = None
    auth_jwt_public_key: str | None = None
    auth_jwt_public_key_file: str | None = None
    auth_jwt_issuer: str = "kubecommerce-auth"
    auth_jwt_audience: str = "kubecommerce"
    auth_access_token_ttl_seconds: int = 900


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()
