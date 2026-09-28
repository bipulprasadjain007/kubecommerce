"""Business logic for auth-service (registration, login, key rotation safety)."""

from __future__ import annotations

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository as repo
from app.config import Settings
from app.models import User
from app.repository import EmailAlreadyExists
from app.schemas import LoginRequest, TokenResponse, UserCreate
from app.security import (
    DUMMY_PASSWORD_HASH,
    SigningKeys,
    create_access_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from kubecommerce_observability import ApiError

_INVALID_CREDENTIALS = ("invalid_credentials", "Invalid email or password")
_EMAIL_TAKEN = ("email_already_registered", "Email is already registered")
_TOKEN_TYPE = "bearer"


async def register_user(session: AsyncSession, payload: UserCreate) -> User:
    """Register a new user, storing the email in lowercase."""
    email = str(payload.email).lower()
    if await repo.get_by_email(session, email) is not None:
        code, message = _EMAIL_TAKEN
        raise ApiError(code, message, status_code=409)

    password_hash = await run_in_threadpool(hash_password, payload.password)
    try:
        return await repo.create_user(
            session,
            email=email,
            password_hash=password_hash,
            full_name=payload.full_name,
        )
    except EmailAlreadyExists as exc:
        code, message = _EMAIL_TAKEN
        raise ApiError(code, message, status_code=409) from exc


async def authenticate(
    session: AsyncSession,
    settings: Settings,
    keys: SigningKeys,
    payload: LoginRequest,
) -> TokenResponse:
    """Verify credentials and issue an access token.

    A dummy verification is always run for unknown emails so response timing and
    behaviour do not reveal whether an account exists.
    """
    email = str(payload.email).lower()
    user = await repo.get_by_email(session, email)
    if user is None:
        await run_in_threadpool(verify_password, payload.password, DUMMY_PASSWORD_HASH)
        code, message = _INVALID_CREDENTIALS
        raise ApiError(code, message, status_code=401)

    verified = await run_in_threadpool(verify_password, payload.password, user.password_hash)
    if not verified or not user.is_active:
        code, message = _INVALID_CREDENTIALS
        raise ApiError(code, message, status_code=401)

    if needs_rehash(user.password_hash):
        new_hash = await run_in_threadpool(hash_password, payload.password)
        await repo.set_password_hash(session, user, new_hash)

    token = create_access_token(user.id, user.email, settings=settings, keys=keys)
    return TokenResponse(
        access_token=token,
        token_type=_TOKEN_TYPE,
        expires_in=settings.auth_access_token_ttl_seconds,
    )
