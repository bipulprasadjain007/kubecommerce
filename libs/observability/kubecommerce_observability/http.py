"""Outbound HTTP client factory with correlation id propagation."""

from __future__ import annotations

from typing import Any

import httpx

from .context import correlation_headers


async def _propagate_correlation_id(request: httpx.Request) -> None:
    """Request event hook that adds ``X-Correlation-ID`` to outbound calls."""
    for key, value in correlation_headers().items():
        request.headers[key] = value


def create_http_client(
    *,
    base_url: str,
    timeout: float | httpx.Timeout,
    transport: httpx.AsyncBaseTransport | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Create an ``httpx.AsyncClient`` that propagates ``X-Correlation-ID``.

    The current context correlation id is echoed on every outbound request; a
    new ``uuid4`` id is generated and stored when none is present. ``traceparent``
    propagation is handled separately by the OpenTelemetry HTTPX instrumentation
    installed by :func:`configure_tracing`
    """
    event_hooks = dict(kwargs.pop("event_hooks", {}) or {})
    request_hooks = list(event_hooks.get("request", []))
    request_hooks.append(_propagate_correlation_id)
    event_hooks["request"] = request_hooks
    kwargs["event_hooks"] = event_hooks

    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        transport=transport,
        **kwargs,
    )
