"""Request proxying with header hygiene and upstream error translation."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping

import httpx
from fastapi import Request
from starlette.responses import Response

from kubecommerce_observability import ApiError

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})

#: Headers a client must never be able to smuggle through the gateway.
STRIPPED_HEADERS = frozenset({"x-user-id", "x-internal-token"})
_STRIPPED_PREFIXES = ("x-forwarded-",)

_MAX_ATTEMPTS = 2


def filtered_headers(
    request: Request,
    extra_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return forwarded request headers with untrusted ones removed.

    Client-supplied ``X-User-ID``, ``X-Internal-Token`` and ``X-Forwarded-*``
    headers are always dropped; ``extra_headers`` (trusted, gateway-derived)
    are applied last so they always win, replacing any client header with the
    same name regardless of case.
    """
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lowered = name.lower()
        if lowered in STRIPPED_HEADERS or lowered.startswith(_STRIPPED_PREFIXES):
            continue
        headers[name] = value
    if extra_headers:
        trusted = {name.lower() for name in extra_headers}
        headers = {name: value for name, value in headers.items() if name.lower() not in trusted}
        headers.update(extra_headers)
    return headers


def _build_response(response: httpx.Response) -> Response:
    """Translate an upstream httpx response into a Starlette response."""
    content_type = response.headers.get("content-type", "application/json")
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers={"content-type": content_type},
    )


async def _sleep_backoff(attempt: int) -> None:
    """Sleep with exponential backoff and jitter before an idempotent retry."""
    await asyncio.sleep(0.05 * (2 ** (attempt - 1)) + random.uniform(0, 0.05))  # noqa: S311


async def forward(
    request: Request,
    upstream_client: httpx.AsyncClient,
    path: str,
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> Response:
    """Forward the current request to an upstream service.

    Forwards method, query string and body, and returns the upstream status and
    body. ``httpx.TimeoutException`` becomes a 504 and ``httpx.RequestError`` a
    502; upstream stack traces and payloads are never exposed. Only ``GET`` and
    ``HEAD`` are retried, at most twice with jittered backoff.
    """
    method = request.method
    url = path
    if request.url.query:
        url = f"{path}?{request.url.query}"
    headers = filtered_headers(request, extra_headers)
    content = await request.body() if method in _BODY_METHODS else None

    attempts = _MAX_ATTEMPTS if method in _IDEMPOTENT_METHODS else 1
    for attempt in range(1, attempts + 1):
        try:
            response = await upstream_client.request(method, url, headers=headers, content=content)
        except httpx.TimeoutException as exc:
            if attempt < attempts:
                await _sleep_backoff(attempt)
                continue
            raise ApiError(
                "upstream_timeout", "Upstream service timed out", status_code=504
            ) from exc
        except httpx.RequestError as exc:
            if attempt < attempts:
                await _sleep_backoff(attempt)
                continue
            raise ApiError(
                "upstream_unavailable", "Upstream service is unavailable", status_code=502
            ) from exc
        return _build_response(response)

    # Unreachable: the loop either returns or raises on the final attempt.
    raise ApiError("upstream_unavailable", "Upstream service is unavailable", status_code=502)
