"""Settings-loading tests."""

from __future__ import annotations

import pytest

from app.config import get_settings


def test_get_settings_reads_environment(
    monkeypatch: pytest.MonkeyPatch, rsa_pem: tuple[str, str]
) -> None:
    private_pem, _ = rsa_pem
    monkeypatch.setenv("AUTH_DATABASE_URL", "sqlite+aiosqlite:///env.db")
    monkeypatch.setenv("AUTH_JWT_PRIVATE_KEY", private_pem)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.auth_database_url == "sqlite+aiosqlite:///env.db"
        assert settings.service_name == "auth-service"
        assert settings.auth_jwt_issuer == "kubecommerce-auth"
        assert settings.auth_jwt_audience == "kubecommerce"
        assert settings.auth_access_token_ttl_seconds == 900
    finally:
        get_settings.cache_clear()
