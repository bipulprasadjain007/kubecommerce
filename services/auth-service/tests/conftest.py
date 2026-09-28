"""Shared fixtures for auth-service tests.

Tests run with no external services: a throwaway RSA keypair is generated in
process and the database is a per-test SQLite file via aiosqlite.

``app.main`` defines a module-level ``app = create_app()`` which loads settings
(and therefore the signing key) at import time. A usable environment is
therefore established here, before any test module imports ``app.main``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import Settings

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")
PUBLIC_PEM = (
    _KEY.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode("ascii")
)

# Required by app.main's import-time create_app(); only used as a fallback for
# Settings construction, which explicitly overrides the key per fixture.
os.environ.setdefault("AUTH_DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("AUTH_JWT_PRIVATE_KEY", PRIVATE_PEM)


@pytest.fixture(scope="session")
def rsa_pem() -> tuple[str, str]:
    """Return a throwaway ``(private_pem, public_pem)`` RSA-2048 keypair."""
    return PRIVATE_PEM, PUBLIC_PEM


@pytest.fixture
def settings(rsa_pem: tuple[str, str], tmp_path: Path) -> Settings:
    """Build auth settings against a per-test SQLite file."""
    private_pem, _ = rsa_pem
    db_path = tmp_path / "auth.db"
    return Settings(
        _env_file=None,
        auth_database_url=f"sqlite+aiosqlite:///{db_path}",
        auth_jwt_private_key=private_pem,
    )


@pytest_asyncio.fixture
async def running_app(settings: Settings) -> AsyncIterator[FastAPI]:
    """Create the app, run lifespan, and create the schema on the test DB."""
    from app.main import create_app
    from app.models import Base

    app = create_app(settings)
    async with LifespanManager(app):
        engine = app.state.engine
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield app


@pytest_asyncio.fixture
async def client(running_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An httpx client bound to the app via ASGI transport (lifespan running)."""
    transport = ASGITransport(app=running_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client
