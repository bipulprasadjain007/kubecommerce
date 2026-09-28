"""Shared test fixtures for the gateway service (no external services)."""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

# The module-level ``app`` in app.main builds settings at import time and the
# internal token is intentionally a required secret (no default in code).
os.environ.setdefault("GATEWAY_INTERNAL_API_TOKEN", "test-internal-token")
os.environ.setdefault("ENVIRONMENT", "dev")

import fakeredis.aioredis
import httpx
import jwt
import pytest
import respx
from asgi_lifespan import LifespanManager
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app

AUTH_BASE = "http://auth-service"
CATALOG_BASE = "http://catalog-service"
ORDER_BASE = "http://order-service"
JWKS_URL = f"{AUTH_BASE}/.well-known/jwks.json"
ISSUER = "kubecommerce-auth"
AUDIENCE = "kubecommerce"


class BrokenRedis:
    """Redis stub that simulates a connection outage for fail-open tests."""

    async def eval(self, *args: Any, **kwargs: Any) -> Any:
        import redis.exceptions

        raise redis.exceptions.ConnectionError("redis unavailable")

    async def ping(self) -> bool:
        import redis.exceptions

        raise redis.exceptions.ConnectionError("redis unavailable")

    async def aclose(self) -> None:
        return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


def make_settings(**overrides: Any) -> Settings:
    """Build isolated test settings for the gateway."""
    base: dict[str, Any] = {
        "service_name": "gateway-api",
        "gateway_redis_url": "redis://localhost:6379/2",
        "gateway_auth_base_url": AUTH_BASE,
        "gateway_catalog_base_url": CATALOG_BASE,
        "gateway_order_base_url": ORDER_BASE,
        "gateway_jwks_url": JWKS_URL,
        "gateway_jwt_issuer": ISSUER,
        "gateway_jwt_audience": AUDIENCE,
        "gateway_rate_limit_per_minute": 120,
        "gateway_request_timeout_seconds": 5.0,
        "gateway_internal_api_token": "test-internal-token",
        "gateway_jwks_cache_ttl_seconds": 300,
    }
    base.update(overrides)
    return Settings(**base)


@asynccontextmanager
async def running_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield an ASGI-transport HTTP client."""
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.fixture(scope="session")
def rsa_private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def kid() -> str:
    return "test-key-1"


@pytest.fixture
def jwks(rsa_private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    return {"keys": [_public_jwk(rsa_private_key, kid)]}


@pytest.fixture
def jwk_factory() -> Callable[[rsa.RSAPrivateKey, str], dict[str, str]]:
    return _public_jwk


@pytest.fixture
def make_token(rsa_private_key: rsa.RSAPrivateKey, kid: str) -> Callable[..., str]:
    def _make(
        *,
        sub: str = "user-123",
        email: str = "user@example.com",
        exp_seconds: int = 900,
        issuer: str = ISSUER,
        audience: str = AUDIENCE,
        key: Any = None,
        kid_override: str | None = None,
    ) -> str:
        now = datetime.now(UTC)
        claims: dict[str, Any] = {
            "sub": sub,
            "email": email,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + timedelta(seconds=exp_seconds),
        }
        signing_key = key if key is not None else rsa_private_key
        return jwt.encode(
            claims, signing_key, algorithm="RS256", headers={"kid": kid_override or kid}
        )

    return _make


@pytest.fixture
def http_mock() -> Any:
    """Activate a respx router that intercepts all httpx requests in the test."""
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def jwks_route(http_mock: Any, jwks: dict[str, Any]) -> Any:
    return http_mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks))


@pytest.fixture
async def fake_redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def broken_redis() -> BrokenRedis:
    return BrokenRedis()


@pytest.fixture
async def make_app(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> AsyncIterator[Callable[..., FastAPI]]:
    def _make(
        redis_client: Any = None,
        **overrides: Any,
    ) -> FastAPI:
        client = redis_client if redis_client is not None else fake_redis
        monkeypatch.setattr("app.main.aioredis.from_url", lambda *a, **k: client)
        return create_app(make_settings(**overrides))

    yield _make


@pytest.fixture
async def default_app(make_app: Callable[..., FastAPI], jwks_route: Any) -> FastAPI:
    return make_app()


@pytest.fixture
async def client(default_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with running_client(default_app) as http_client:
        yield http_client
