"""Tests for the outbound HTTP client factory."""

from __future__ import annotations

import httpx

from kubecommerce_observability.context import (
    get_correlation_id,
    reset_context,
    set_correlation_id,
)
from kubecommerce_observability.http import create_http_client


async def test_create_http_client_echoes_existing_correlation_id() -> None:
    set_correlation_id("ctx-123")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    async with create_http_client(
        base_url="http://svc",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    ) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200
    assert captured[0].headers["X-Correlation-ID"] == "ctx-123"
    assert get_correlation_id() == "ctx-123"
    reset_context()


async def test_create_http_client_generates_correlation_id_when_absent() -> None:
    reset_context()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    async with create_http_client(
        base_url="http://svc",
        timeout=httpx.Timeout(5.0),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get("/items/1")

    generated = captured[0].headers["X-Correlation-ID"]
    assert len(generated) == 32
    assert get_correlation_id() == generated
    reset_context()
