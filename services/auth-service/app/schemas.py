"""Pydantic request/response schemas for auth-service."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    """Payload for registering a new user."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)


class UserRead(BaseModel):
    """A user as returned by the API (never includes credentials)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None
    created_at: datetime


class LoginRequest(BaseModel):
    """Payload for exchanging credentials for an access token."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    """A freshly issued bearer access token."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
