"""Public API routes proxied to the auth, catalog and order services."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from app.proxy import forward
from app.ratelimit import enforce_rate_limit
from app.security import get_current_user
from kubecommerce_observability import ApiError

CurrentUser = Annotated[dict[str, Any], Depends(get_current_user)]

router = APIRouter(dependencies=[Depends(enforce_rate_limit)])

_ORDER_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]


async def _forward(
    request: Request,
    client_name: str,
    path: str,
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> Response:
    client = getattr(request.app.state, client_name)
    return await forward(request, client, path, extra_headers=extra_headers)


def _order_headers(request: Request) -> dict[str, str]:
    """Return trusted order-service headers derived from verified claims.

    ``X-User-ID`` comes from the verified token ``sub`` and the internal token
    authenticates the gateway as a trusted service-to-service caller; a client
    supplied ``Idempotency-Key`` is forwarded verbatim.
    """
    user_id = getattr(request.state, "user_id", None)
    if not isinstance(user_id, str) or not user_id:
        raise ApiError("invalid_token", "Token subject is missing", status_code=401)
    token: str = request.app.state.settings.gateway_internal_api_token
    headers = {"X-User-ID": user_id, "X-Internal-Token": token}
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


# --- auth-service -----------------------------------------------------------


@router.post("/api/auth/users")
async def register_user(request: Request) -> Response:
    """Register a new user (public)."""
    return await _forward(request, "auth_client", "/users")


@router.post("/api/auth/login")
async def login(request: Request) -> Response:
    """Authenticate and issue an access token (public)."""
    return await _forward(request, "auth_client", "/login")


@router.get("/api/auth/me")
async def current_user(
    request: Request,
    _claims: CurrentUser,
) -> Response:
    """Return the authenticated user's profile."""
    return await _forward(request, "auth_client", "/me")


# --- catalog-service --------------------------------------------------------


@router.get("/api/catalog/products")
async def list_products(request: Request) -> Response:
    """List products (public)."""
    return await _forward(request, "catalog_client", "/products")


@router.get("/api/catalog/products/{product_id}")
async def get_product(request: Request, product_id: str) -> Response:
    """Fetch a single product (public)."""
    return await _forward(request, "catalog_client", f"/products/{product_id}")


# --- order-service ----------------------------------------------------------


@router.api_route("/api/orders", methods=_ORDER_METHODS)
async def orders_root(
    request: Request,
    _claims: CurrentUser,
) -> Response:
    """Proxy order-service root endpoints with the verified user identity."""
    return await _forward(request, "order_client", "/orders", extra_headers=_order_headers(request))


@router.api_route("/api/orders/{rest:path}", methods=_ORDER_METHODS)
async def orders_subpath(
    request: Request,
    rest: str,
    _claims: CurrentUser,
) -> Response:
    """Proxy order-service subpaths with the verified user identity."""
    return await _forward(
        request, "order_client", f"/orders/{rest}", extra_headers=_order_headers(request)
    )
