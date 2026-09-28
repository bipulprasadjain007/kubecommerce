"""Data access functions for auth-service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


class EmailAlreadyExists(Exception):
    """Raised when a user insert violates the unique email constraint."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    full_name: str | None,
) -> User:
    """Insert a user, translating unique-violations into ``EmailAlreadyExists``."""
    user = User(email=email, password_hash=password_hash, full_name=full_name)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise EmailAlreadyExists from exc
    return user


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    """Return the user with ``email``, if any."""
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Return the user with ``user_id``, if any."""
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def set_password_hash(session: AsyncSession, user: User, password_hash: str) -> None:
    """Replace ``user``'s password hash (used for transparent rehashing)."""
    user.password_hash = password_hash
    user.updated_at = _utcnow()
    await session.commit()
