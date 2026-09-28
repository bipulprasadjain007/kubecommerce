"""Application factory for auth-service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.api.routes import router
from app.config import Settings, get_settings
from app.db import create_engine_and_session_factory
from app.security import load_signing_keys
from kubecommerce_observability import install_observability, shutdown_tracing


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the auth-service FastAPI application.

    Signing keys are loaded eagerly so a missing/invalid private key fails app
    creation rather than the first login.
    """
    settings = settings or get_settings()
    signing_keys = load_signing_keys(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine, session_factory = create_engine_and_session_factory(settings.auth_database_url)
        app.state.engine = engine
        app.state.session_factory = session_factory
        try:
            yield
        finally:
            await engine.dispose()
            shutdown_tracing()

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        lifespan=lifespan,
    )

    async def db_ready() -> bool:
        engine = getattr(app.state, "engine", None)
        if engine is None:
            return False
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    metrics = install_observability(
        app,
        settings=settings,
        readiness_checks=[("database", db_ready)],
        startup_checks=[],
    )
    app.state.settings = settings
    app.state.signing_keys = signing_keys
    app.state.metrics = metrics
    app.include_router(router)
    return app


app = create_app()
