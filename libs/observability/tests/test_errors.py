"""Tests for the standard API error handling."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from conftest import parse_json_lines
from fastapi import FastAPI
from httpx import AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from kubecommerce_observability.errors import ApiError, error_response, register_exception_handlers
from kubecommerce_observability.logging import configure_logging
from kubecommerce_observability.middleware import CorrelationIdMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    register_exception_handlers(app)

    @app.get("/api-error")
    async def api_error() -> None:
        raise ApiError("not_found", "Item missing", status_code=404, details={"id": 1})

    @app.get("/items/{count}")
    async def item_count(count: int) -> dict[str, int]:
        return {"count": count}

    @app.get("/forbidden")
    async def forbidden() -> None:
        raise StarletteHTTPException(status_code=403, detail="Nope")

    @app.get("/broken")
    async def broken() -> None:
        raise RuntimeError("super secret traceback")

    return app


async def test_api_error_envelope(make_client: Callable[..., AsyncClient]) -> None:
    async with make_client(_build_app()) as client:
        response = await client.get("/api-error", headers={"X-Correlation-ID": "cid-1"})

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "Item missing"
    assert body["error"]["correlation_id"] == "cid-1"
    assert body["error"]["details"] == {"id": 1}
    assert response.headers["X-Correlation-ID"] == "cid-1"


async def test_validation_error_shape(make_client: Callable[..., AsyncClient]) -> None:
    async with make_client(_build_app()) as client:
        response = await client.get("/items/not-an-int")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["correlation_id"] is not None
    assert isinstance(error["details"], list)
    assert error["details"]
    assert {"loc", "msg", "type"} <= set(error["details"][0])


async def test_http_exception_shape(make_client: Callable[..., AsyncClient]) -> None:
    async with make_client(_build_app()) as client:
        response = await client.get("/forbidden")

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "http_error"
    assert error["message"] == "Nope"


async def test_generic_exception_shape_hides_traceback(
    make_client: Callable[..., AsyncClient],
) -> None:
    async with make_client(_build_app()) as client:
        response = await client.get("/broken", headers={"X-Correlation-ID": "cid-err"})

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "Internal server error"
    assert body["error"]["correlation_id"] == "cid-err"
    assert response.headers["X-Correlation-ID"] == "cid-err"
    assert "super secret traceback" not in response.text
    assert "Traceback" not in response.text


def test_error_response_public_builder() -> None:
    """Services can build the standard envelope for custom responses."""
    response = error_response(None, 429, "rate_limited", "Too many requests", {"retry_after": 5})

    assert response.status_code == 429
    body = json.loads(response.body)
    assert body["error"]["code"] == "rate_limited"
    assert body["error"]["message"] == "Too many requests"
    assert body["error"]["details"] == {"retry_after": 5}


def test_api_error_attributes() -> None:
    error = ApiError("bad", "Bad request")
    assert error.code == "bad"
    assert error.message == "Bad request"
    assert error.status_code == 400
    assert error.details is None
    assert str(error) == "Bad request"


async def test_generic_exception_error_log_is_secret_safe(
    make_client: Callable[..., AsyncClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("errors-secret", "dev", level="INFO")

    async with make_client(_build_app()) as client:
        response = await client.get("/broken", headers={"X-Correlation-ID": "secret-corr"})

    assert response.status_code == 500
    assert response.json()["error"]["correlation_id"] == "secret-corr"

    records = parse_json_lines(capsys.readouterr().out)
    error_record = next(
        item
        for item in records
        if item.get("level") == "error" and item.get("event") == "unhandled_exception"
    )
    assert error_record["error_type"] == "RuntimeError"
    assert error_record["correlation_id"] == "secret-corr"

    rendered = json.dumps(error_record)
    assert "super secret traceback" not in rendered
    assert "traceback" not in rendered.lower()
    assert "exception" not in error_record
