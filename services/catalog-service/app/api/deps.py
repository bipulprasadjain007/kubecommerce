"""API dependencies: DB session, Redis cache and internal-token auth."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator

from fastapi import Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import CatalogCache
from kubecommerce_observability import ApiError


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a database session, rolling back on error."""
    async with request.app.state.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_cache(request: Request) -> CatalogCache:
    """Return the application-scoped catalog cache."""
    return request.app.state.cache


async def require_internal_token(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """Reject requests without a valid ``X-Internal-Token``."""
    expected = request.app.state.settings.catalog_internal_api_token
    if x_internal_token is None:
        raise ApiError("missing_internal_token", "Internal token is required", status_code=401)
    if not hmac.compare_digest(x_internal_token, expected):
        raise ApiError("invalid_internal_token", "Internal token is invalid", status_code=401)
