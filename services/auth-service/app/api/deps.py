"""API dependencies: DB session and bearer-token authentication."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository as repo
from app.models import User
from app.security import (
    SigningKeys,
    TokenExpiredError,
    TokenInvalidError,
    decode_access_token,
)
from kubecommerce_observability import ApiError

_BEARER_PREFIX = "Bearer "


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a database session, rolling back on error."""
    async with request.app.state.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_signing_keys(request: Request) -> SigningKeys:
    """Return the application-scoped parsed signing keys."""
    return request.app.state.signing_keys


async def get_current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> User:
    """Resolve the bearer token to an active user, or raise the right 401."""
    if authorization is None or not authorization.startswith(_BEARER_PREFIX):
        raise ApiError("missing_token", "Authorization bearer token is required", status_code=401)
    token = authorization[len(_BEARER_PREFIX) :]
    try:
        claims = decode_access_token(
            token,
            settings=request.app.state.settings,
            keys=request.app.state.signing_keys,
        )
    except TokenExpiredError as exc:
        raise ApiError("expired_token", "Access token has expired", status_code=401) from exc
    except TokenInvalidError as exc:
        raise ApiError("invalid_token", "Access token is invalid", status_code=401) from exc

    try:
        user_id = uuid.UUID(claims.sub)
    except ValueError as exc:
        raise ApiError("invalid_token", "Access token is invalid", status_code=401) from exc

    user = await repo.get_by_id(session, user_id)
    if user is None or not user.is_active:
        raise ApiError("invalid_token", "Access token is invalid", status_code=401)
    return user
