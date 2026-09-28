"""End-to-end HTTP tests for the auth-service API via ASGI transport."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import repository as repo
from app.api.deps import get_signing_keys
from app.config import Settings
from app.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    decode_access_token,
    verify_password,
)

_PASSWORD = "correct-horse-42"


async def _register(
    client: AsyncClient,
    email: str = "user@example.com",
    password: str = _PASSWORD,
    full_name: str | None = "Test User",
) -> httpx.Response:
    return await client.post(
        "/users",
        json={"email": email, "password": password, "full_name": full_name},
    )


async def _login(client: AsyncClient, email: str, password: str) -> httpx.Response:
    return await client.post("/login", json={"email": email, "password": password})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- registration ------------------------------------------------------------


async def test_register_returns_201_profile(client: AsyncClient) -> None:
    response = await _register(client, email="new@example.com", full_name="New User")
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "full_name", "created_at"}
    assert body["email"] == "new@example.com"
    assert body["full_name"] == "New User"
    assert uuid.UUID(body["id"])


async def test_register_lowercases_email(client: AsyncClient) -> None:
    response = await _register(client, email="MixedCase@Example.COM")
    assert response.status_code == 201
    assert response.json()["email"] == "mixedcase@example.com"
    login = await _login(client, "MIXEDCASE@example.com", _PASSWORD)
    assert login.status_code == 200


async def test_duplicate_email_returns_409(client: AsyncClient) -> None:
    assert (await _register(client, email="dup@example.com")).status_code == 201
    duplicate = await _register(client, email="DUP@example.com")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "email_already_registered"


async def test_short_password_returns_422(client: AsyncClient) -> None:
    response = await _register(client, email="short@example.com", password="short")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_long_full_name_returns_422(client: AsyncClient) -> None:
    response = await _register(client, full_name="x" * 201)
    assert response.status_code == 422


async def test_invalid_email_returns_422(client: AsyncClient) -> None:
    response = await _register(client, email="not-an-email")
    assert response.status_code == 422


async def test_register_integrity_race_returns_409(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    from app import service as service_module
    from app.repository import EmailAlreadyExists

    async def _no_existing(*_args: object, **_kwargs: object) -> None:
        return None

    async def _conflict(*_args: object, **_kwargs: object) -> None:
        raise EmailAlreadyExists

    monkeypatch.setattr(service_module.repo, "get_by_email", _no_existing)
    monkeypatch.setattr(service_module.repo, "create_user", _conflict)
    response = await _register(client, email="race@example.com")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_registered"


# --- login -------------------------------------------------------------------


async def test_login_returns_bearer_token(client: AsyncClient, running_app: FastAPI) -> None:
    await _register(client, email="login@example.com")
    response = await _login(client, "login@example.com", _PASSWORD)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == running_app.state.settings.auth_access_token_ttl_seconds
    claims = decode_access_token(
        body["access_token"],
        settings=running_app.state.settings,
        keys=running_app.state.signing_keys,
    )
    assert claims.email == "login@example.com"


async def test_login_wrong_password_returns_401(client: AsyncClient) -> None:
    await _register(client, email="wrong@example.com")
    response = await _login(client, "wrong@example.com", "not-the-password")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_unknown_email_returns_401(client: AsyncClient) -> None:
    response = await _login(client, "nobody@example.com", _PASSWORD)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_unknown_email_runs_dummy_verify(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    from app import service as service_module

    calls: list[tuple[str, str]] = []

    def _spy(password: str, password_hash: str) -> bool:
        calls.append((password, password_hash))
        return False

    monkeypatch.setattr(service_module, "verify_password", _spy)
    response = await _login(client, "nobody@example.com", _PASSWORD)
    assert response.status_code == 401
    assert calls == [(_PASSWORD, DUMMY_PASSWORD_HASH)]


async def test_login_inactive_user_returns_401(client: AsyncClient, running_app: FastAPI) -> None:
    await _register(client, email="inactive@example.com")
    async with running_app.state.session_factory() as session:
        user = await repo.get_by_email(session, "inactive@example.com")
        assert user is not None
        user.is_active = False
        await session.commit()
    response = await _login(client, "inactive@example.com", _PASSWORD)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_rehashes_outdated_password(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient, running_app: FastAPI
) -> None:
    from app import service as service_module

    await _register(client, email="rehash@example.com")
    async with running_app.state.session_factory() as session:
        user = await repo.get_by_email(session, "rehash@example.com")
        assert user is not None
        old_hash = user.password_hash

    monkeypatch.setattr(service_module, "needs_rehash", lambda _hash: True)
    assert (await _login(client, "rehash@example.com", _PASSWORD)).status_code == 200

    async with running_app.state.session_factory() as session:
        refreshed = await repo.get_by_email(session, "rehash@example.com")
        assert refreshed is not None
        assert refreshed.password_hash != old_hash
        assert verify_password(_PASSWORD, refreshed.password_hash)


# --- /me ---------------------------------------------------------------------


async def test_me_returns_profile(client: AsyncClient) -> None:
    await _register(client, email="me@example.com", full_name="Me")
    token = (await _login(client, "me@example.com", _PASSWORD)).json()["access_token"]
    response = await client.get("/me", headers=_bearer(token))
    assert response.status_code == 200
    assert response.json()["email"] == "me@example.com"
    assert response.json()["full_name"] == "Me"


async def test_me_without_token_returns_missing_token(client: AsyncClient) -> None:
    response = await client.get("/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_token"


async def test_me_with_non_bearer_scheme_returns_missing_token(client: AsyncClient) -> None:
    response = await client.get("/me", headers={"Authorization": "Token abc"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_token"


async def test_me_with_garbage_token_returns_invalid_token(client: AsyncClient) -> None:
    response = await client.get("/me", headers=_bearer("not-a-jwt"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_me_with_expired_token_returns_expired_token(
    client: AsyncClient, running_app: FastAPI
) -> None:
    settings = running_app.state.settings
    expired_settings = settings.model_copy(update={"auth_access_token_ttl_seconds": -10})
    token = create_access_token(
        uuid.uuid4(),
        "expired@example.com",
        settings=expired_settings,
        keys=running_app.state.signing_keys,
    )
    response = await client.get("/me", headers=_bearer(token))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "expired_token"


async def test_me_with_unknown_user_token_returns_invalid_token(
    client: AsyncClient, running_app: FastAPI
) -> None:
    token = create_access_token(
        uuid.uuid4(),
        "ghost@example.com",
        settings=running_app.state.settings,
        keys=running_app.state.signing_keys,
    )
    response = await client.get("/me", headers=_bearer(token))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_me_with_non_uuid_subject_returns_invalid_token(
    client: AsyncClient, running_app: FastAPI
) -> None:
    token = create_access_token(
        "not-a-uuid",
        "subject@example.com",
        settings=running_app.state.settings,
        keys=running_app.state.signing_keys,
    )
    response = await client.get("/me", headers=_bearer(token))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


# --- JWKS, health, metrics ---------------------------------------------------


async def test_jwks_endpoint_matches_signing_key(client: AsyncClient, running_app: FastAPI) -> None:
    response = await client.get("/.well-known/jwks.json")
    assert response.status_code == 200
    keys = response.json()["keys"]
    assert len(keys) == 1
    assert keys[0]["kid"] == running_app.state.signing_keys.kid
    assert keys[0]["alg"] == "RS256"


async def test_readiness_is_ok_with_database(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"


async def test_readiness_before_lifespan_is_503(settings: Settings) -> None:
    from app.main import create_app

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        response = await http_client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "failed"


async def test_readiness_with_unreachable_database_is_503(settings: Settings) -> None:
    from app.main import create_app

    bad_settings = settings.model_copy(
        update={"auth_database_url": "sqlite+aiosqlite:///no-such-dir-xyz/auth.db"}
    )
    app = create_app(bad_settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            response = await http_client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "failed"


async def test_metrics_endpoint_is_exposed(client: AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "kubecommerce" in response.text


# --- secret hygiene ----------------------------------------------------------


async def test_responses_do_not_leak_secrets(client: AsyncClient) -> None:
    password = "SuperSecret-Passw0rd"
    register = await _register(client, email="leak@example.com", password=password)
    assert password not in register.text
    assert "password_hash" not in register.text
    assert "password" not in register.json()

    login = await _login(client, "leak@example.com", password)
    assert password not in login.text
    token = login.json()["access_token"]

    me = await client.get("/me", headers=_bearer(token))
    assert password not in me.text
    assert "password_hash" not in me.text


# --- dependency units --------------------------------------------------------


def test_get_signing_keys_reads_app_state() -> None:
    sentinel = object()
    app = FastAPI()
    app.state.signing_keys = sentinel
    request = SimpleNamespace(app=app)
    assert get_signing_keys(request) is sentinel  # type: ignore[arg-type]
