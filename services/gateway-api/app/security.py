"""JWT authentication: JWKS caching, verification and request dependencies."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import jwt
from fastapi import Request
from jwt.algorithms import RSAAlgorithm

from kubecommerce_observability import ApiError, get_logger

logger = get_logger("gateway.security")

_ALGORITHM = "RS256"
_DEFAULT_MIN_REFRESH_INTERVAL = 10.0


def _extract_bearer_token(request: Request) -> str | None:
    """Return the bearer token from the ``Authorization`` header, if present."""
    header = request.headers.get("Authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _apply_claims(request: Request, claims: dict[str, Any]) -> None:
    """Store verified claims and identity on the request state."""
    request.state.claims = claims
    request.state.user_id = claims.get("sub")
    request.state.email = claims.get("email")


class JWKSVerifier:
    """Verify RS256 JWTs against a cached JWKS document.

    Signing keys are cached by ``kid`` for ``cache_ttl`` seconds. An unknown
    ``kid`` triggers a single re-fetch (coalesced under a lock and rate-limited
    by ``min_refresh_interval``) so key rotation is handled without a thundering
    herd of JWKS requests.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        jwks_url: str,
        issuer: str,
        audience: str,
        cache_ttl: float = 300.0,
        min_refresh_interval: float = _DEFAULT_MIN_REFRESH_INTERVAL,
    ) -> None:
        self._client = client
        self._jwks_url = jwks_url
        self._issuer = issuer
        self._audience = audience
        self._cache_ttl = cache_ttl
        self._min_refresh_interval = min_refresh_interval
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._last_refresh_attempt = 0.0
        self._lock = asyncio.Lock()

    def _cache_fresh(self, now: float) -> bool:
        """Return True when the cached key set is present and not stale."""
        return bool(self._keys) and (now - self._fetched_at) < self._cache_ttl

    async def _load_jwks(self, *, force: bool) -> None:
        """Fetch and cache the JWKS document.

        ``force`` requests a refresh even when the cache is fresh; the refresh
        is skipped if another refresh happened within ``min_refresh_interval``.
        """
        async with self._lock:
            now = time.monotonic()
            if not force and self._cache_fresh(now):
                return
            if (
                force
                and self._keys
                and (now - self._last_refresh_attempt) < self._min_refresh_interval
            ):
                return
            if force:
                # Only forced (unknown-kid) refreshes are rate-limited so a
                # fresh cache load does not suppress the first rotation lookup.
                self._last_refresh_attempt = now
            try:
                response = await self._client.get(self._jwks_url)
            except httpx.RequestError as exc:
                logger.warning("jwks_fetch_failed", error_type=type(exc).__name__)
                raise ApiError(
                    "invalid_token", "Unable to verify authentication token", status_code=401
                ) from exc
            if response.status_code != 200:
                logger.warning("jwks_fetch_unexpected_status", status_code=response.status_code)
                raise ApiError(
                    "invalid_token", "Unable to verify authentication token", status_code=401
                )
            payload = response.json()
            keys: dict[str, Any] = {}
            for item in payload.get("keys", []):
                if isinstance(item, dict) and isinstance(item.get("kid"), str):
                    keys[item["kid"]] = item
            self._keys = keys
            self._fetched_at = time.monotonic()

    async def _resolve_key(self, kid: str | None) -> Any:
        """Return the signing key for ``kid``, refreshing the cache as needed."""
        now = time.monotonic()
        if self._cache_fresh(now):
            if kid is not None:
                key = self._keys.get(kid)
                if key is not None:
                    return key
            elif self._keys:
                return next(iter(self._keys.values()))
            # Unknown kid: attempt exactly one re-fetch, rate-limited.
            if (now - self._last_refresh_attempt) < self._min_refresh_interval:
                return None
            await self._load_jwks(force=True)
        else:
            await self._load_jwks(force=False)

        if kid is not None:
            return self._keys.get(kid)
        if self._keys:
            return next(iter(self._keys.values()))
        return None

    async def verify(self, token: str) -> dict[str, Any]:
        """Verify ``token`` and return its claims.

        Raises :class:`ApiError` with ``invalid_token`` or ``expired_token``.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise ApiError(
                "invalid_token", "Invalid authentication token", status_code=401
            ) from exc

        key = await self._resolve_key(header.get("kid"))
        if key is None:
            raise ApiError("invalid_token", "Unknown authentication token key", status_code=401)

        try:
            public_key: Any = RSAAlgorithm.from_jwk(key)
            claims = jwt.decode(
                token,
                key=public_key,
                algorithms=[_ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise ApiError(
                "expired_token", "Authentication token has expired", status_code=401
            ) from exc
        except jwt.InvalidTokenError as exc:
            raise ApiError(
                "invalid_token", "Invalid authentication token", status_code=401
            ) from exc
        return dict(claims)


async def get_current_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency requiring a valid bearer token.

    Sets ``request.state.user_id`` and ``request.state.email``.
    """
    cached = getattr(request.state, "claims", None)
    if isinstance(cached, dict):
        return cached

    token = _extract_bearer_token(request)
    if token is None:
        raise ApiError("missing_token", "Bearer token is required", status_code=401)

    verifier: JWKSVerifier = request.app.state.jwks_verifier
    claims = await verifier.verify(token)
    _apply_claims(request, claims)
    return claims


async def authenticate_if_present(request: Request) -> dict[str, Any] | None:
    """Verify the bearer token when one is supplied, otherwise return None.

    Used for identity-aware rate limiting on routes that may be public; a
    presented but invalid token is still rejected.
    """
    cached = getattr(request.state, "claims", None)
    if isinstance(cached, dict):
        return cached

    token = _extract_bearer_token(request)
    if token is None:
        return None

    verifier: JWKSVerifier = request.app.state.jwks_verifier
    claims = await verifier.verify(token)
    _apply_claims(request, claims)
    return claims
