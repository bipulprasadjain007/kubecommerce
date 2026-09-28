"""Base settings shared by all KubeCommerce services."""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Common service configuration loaded from the environment and ``.env``.

    The environment variable name for a field is the uppercase field name,
    for example ``environment`` -> ``ENVIRONMENT``.
    """

    environment: Literal["dev", "staging", "prod"] = "dev"
    service_name: str = "unknown-service"
    log_level: str = "INFO"
    service_version: str = "0.0.0"
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str | None = None
    otel_sample_ratio: float = 1.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def is_production(self) -> bool:
        """Return True when the service is running in production."""
        return self.environment == "prod"
