"""HTTP routes for auth-service."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import service
from app.api.deps import get_current_user, get_session
from app.models import User
from app.schemas import LoginRequest, TokenResponse, UserCreate, UserRead
from app.security import build_jwks

router = APIRouter()

type SessionDep = Annotated[AsyncSession, Depends(get_session)]
type CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register_user(payload: UserCreate, session: SessionDep) -> UserRead:
    """Register a new user account."""
    user = await service.register_user(session, payload)
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, session: SessionDep) -> TokenResponse:
    """Exchange credentials for a bearer access token."""
    return await service.authenticate(
        session,
        request.app.state.settings,
        request.app.state.signing_keys,
        payload,
    )


@router.get("/me", response_model=UserRead)
async def read_me(user: CurrentUser) -> UserRead:
    """Return the authenticated user's profile."""
    return UserRead.model_validate(user)


@router.get("/.well-known/jwks.json")
async def jwks(request: Request) -> dict[str, Any]:
    """Expose the public signing key set (no authentication)."""
    return build_jwks(
        settings=request.app.state.settings,
        keys=request.app.state.signing_keys,
    )
