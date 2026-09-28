"""Tests for settings loading."""

from __future__ import annotations

from app.config import Settings, get_settings


def test_get_settings_uses_canonical_service_name() -> None:
    assert get_settings().service_name == "order-service"


def test_settings_defaults() -> None:
    settings = Settings(
        orders_database_url="sqlite+aiosqlite:///:memory:",
        orders_rabbitmq_url="amqp://guest:guest@localhost/",
        orders_catalog_base_url="http://catalog.test",
        orders_internal_api_token="token",
    )

    assert settings.orders_request_timeout_seconds == 5.0
    assert settings.orders_outbox_poll_interval_seconds == 2.0
    assert settings.orders_outbox_batch_size == 20
    assert settings.environment == "dev"
