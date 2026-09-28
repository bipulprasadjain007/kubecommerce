"""FastAPI dependencies for the order API."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.catalog import CatalogClient
from kubecommerce_observability import ApiError

MAX_IDEMPOTENCY_KEY_LENGTH = 128


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a database session, rolling back on error."""
    async with request.app.state.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_catalog(request: Request) -> CatalogClient:
    """Return the process-wide catalog client."""
    catalog: CatalogClient = request.app.state.catalog
    return catalog


async def require_internal_token(
    request: Request,
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> None:
    """Reject requests without a valid ``X-Internal-Token``."""
    expected: str = request.app.state.settings.orders_internal_api_token
    if x_internal_token is None:
        raise ApiError("missing_internal_token", "Internal token is required", status_code=401)
    if not hmac.compare_digest(x_internal_token, expected):
        raise ApiError("invalid_internal_token", "Internal token is invalid", status_code=401)


async def get_current_user_id(
    x_user_id: Annotated[str | None, Header(alias="X-User-ID")] = None,
) -> UUID:
    """Resolve the gateway-authenticated user id from ``X-User-ID``."""
    if x_user_id is None:
        raise ApiError("missing_user_context", "X-User-ID header is required", status_code=401)
    try:
        return UUID(x_user_id)
    except ValueError as exc:
        raise ApiError(
            "missing_user_context",
            "X-User-ID header must be a valid UUID",
            status_code=401,
        ) from exc


async def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    """Validate the optional ``Idempotency-Key`` header."""
    if idempotency_key is None:
        return None
    if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ApiError(
            "invalid_idempotency_key",
            "Idempotency-Key must be between 1 and 128 characters",
            status_code=400,
        )
    return idempotency_key
