"""Tests for shared service settings."""

from __future__ import annotations

import pytest

from kubecommerce_observability.settings import BaseServiceSettings

_SHARED_ENV = (
    "ENVIRONMENT",
    "SERVICE_NAME",
    "LOG_LEVEL",
    "SERVICE_VERSION",
    "OTEL_ENABLED",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_SAMPLE_RATIO",
)


@pytest.fixture(autouse=True)
def _clear_shared_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SHARED_ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults() -> None:
    settings = BaseServiceSettings(_env_file=None)
    assert settings.environment == "dev"
    assert settings.service_name == "unknown-service"
    assert settings.log_level == "INFO"
    assert settings.service_version == "0.0.0"
    assert settings.otel_enabled is False
    assert settings.otel_exporter_otlp_endpoint is None
    assert settings.otel_sample_ratio == 1.0
    assert settings.is_production is False


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("SERVICE_NAME", "order-service")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("SERVICE_VERSION", "2.1.0")
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4318")
    monkeypatch.setenv("OTEL_SAMPLE_RATIO", "0.25")

    settings = BaseServiceSettings(_env_file=None)
    assert settings.environment == "prod"
    assert settings.is_production is True
    assert settings.service_name == "order-service"
    assert settings.log_level == "DEBUG"
    assert settings.service_version == "2.1.0"
    assert settings.otel_enabled is True
    assert settings.otel_exporter_otlp_endpoint == "http://otel:4318"
    assert settings.otel_sample_ratio == 0.25


def test_invalid_environment_rejected() -> None:
    with pytest.raises(ValueError):
        BaseServiceSettings(environment="not-an-env", _env_file=None)
