"""Proxy and header hygiene tests."""

from __future__ import annotations

import httpx
import pytest
from starlette.requests import Request

from app.proxy import filtered_headers, forward


def _request(headers: list[tuple[bytes, bytes]], method: str = "GET") -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "query_string": b"",
        "headers": headers,
    }
    return Request(scope)


def test_filtered_headers_removes_untrusted_and_applies_extra() -> None:
    request = _request(
        [
            (b"x-user-id", b"attacker"),
            (b"x-internal-token", b"evil"),
            (b"x-forwarded-for", b"9.9.9.9"),
            (b"accept", b"application/json"),
        ]
    )

    headers = filtered_headers(request, {"X-User-ID": "trusted-sub"})

    assert headers["X-User-ID"] == "trusted-sub"
    assert headers["accept"] == "application/json"
    assert "X-Internal-Token" not in headers
    assert "X-Forwarded-For" not in headers


def test_filtered_headers_replaces_extra_case_insensitively() -> None:
    request = _request([(b"idempotency-key", b"client-value")])

    headers = filtered_headers(request, {"Idempotency-Key": "trusted-value"})

    # The trusted value must replace, not duplicate, the client header.
    assert headers == {"Idempotency-Key": "trusted-value"}


async def test_forward_requires_no_extra_headers() -> None:
    request = _request([(b"accept", b"application/json")], method="GET")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://upstream", transport=transport) as client:
        response = await forward(request, client, "/things")

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (httpx.ConnectTimeout("timeout"), 504, "upstream_timeout"),
        (httpx.ConnectError("refused"), 502, "upstream_unavailable"),
    ],
)
async def test_forward_translates_upstream_errors(exc: Exception, status: int, code: str) -> None:
    request = _request([], method="GET")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://upstream", transport=transport) as client:
        with pytest.raises(Exception) as caught:
            await forward(request, client, "/things")

    assert getattr(caught.value, "status_code", None) == status
    assert getattr(caught.value, "code", None) == code
