"""Tests for correlation id and access log middleware."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import parse_json_lines
from fastapi import FastAPI
from httpx import AsyncClient

from kubecommerce_observability.errors import ApiError, register_exception_handlers
from kubecommerce_observability.logging import configure_logging
from kubecommerce_observability.middleware import (
    AccessLogMiddleware,
    CorrelationIdMiddleware,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AccessLogMiddleware, service_name="svc")
    app.add_middleware(CorrelationIdMiddleware)
    register_exception_handlers(app)

    @app.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics_route() -> dict[str, str]:
        return {"metrics": "ok"}

    @app.get("/fail")
    async def fail() -> None:
        raise ApiError("boom", "failed", status_code=409)

    return app


async def test_valid_correlation_id_is_echoed(
    make_client: Callable[..., AsyncClient],
) -> None:
    async with make_client(_build_app()) as client:
        response = await client.get("/items/1", headers={"X-Correlation-ID": "client-123"})
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "client-123"


async def test_missing_correlation_id_is_generated(
    make_client: Callable[..., AsyncClient],
) -> None:
    async with make_client(_build_app()) as client:
        response = await client.get("/items/1")
    generated = response.headers["X-Correlation-ID"]
    assert len(generated) == 32
    assert generated != ""


async def test_invalid_correlation_id_is_replaced(
    make_client: Callable[..., AsyncClient],
) -> None:
    async with make_client(_build_app()) as client:
        response = await client.get("/items/1", headers={"X-Correlation-ID": "bad id with spaces!"})
    generated = response.headers["X-Correlation-ID"]
    assert generated != "bad id with spaces!"
    assert len(generated) == 32


async def test_correlation_header_present_on_error_response(
    make_client: Callable[..., AsyncClient],
) -> None:
    async with make_client(_build_app()) as client:
        response = await client.get("/fail", headers={"X-Correlation-ID": "err-42"})
    assert response.status_code == 409
    assert response.headers["X-Correlation-ID"] == "err-42"


async def test_access_log_uses_route_template(
    make_client: Callable[..., AsyncClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("access-log-test", "dev")

    async with make_client(_build_app()) as client:
        await client.get("/items/abc-123", headers={"X-Correlation-ID": "corr-access"})

    records = parse_json_lines(capsys.readouterr().out)
    record = next(item for item in records if item.get("event") == "http_request")
    assert record["path"] == "/items/{item_id}"
    assert "abc-123" not in record["path"]
    assert record["method"] == "GET"
    assert record["status_code"] == 200
    assert isinstance(record["duration_ms"], float)
    assert record["correlation_id"] == "corr-access"
    assert record["service"] == "svc"


async def test_access_log_excludes_health_and_metrics(
    make_client: Callable[..., AsyncClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("access-log-exclude", "dev")

    async with make_client(_build_app()) as client:
        await client.get("/health/live")
        await client.get("/metrics")
        await client.get("/items/7")

    records = parse_json_lines(capsys.readouterr().out)
    access_paths = [item["path"] for item in records if item.get("event") == "http_request"]
    assert access_paths == ["/items/{item_id}"]


async def test_access_log_marks_unmatched_route(
    make_client: Callable[..., AsyncClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("access-log-unmatched", "dev")

    async with make_client(_build_app()) as client:
        await client.get("/does/not/exist")

    records = parse_json_lines(capsys.readouterr().out)
    record = next(item for item in records if item.get("event") == "http_request")
    assert record["path"] == "unmatched"
    assert record["status_code"] == 404
    assert "does" not in record["path"]


def _build_raising_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AccessLogMiddleware, service_name="svc")
    app.add_middleware(CorrelationIdMiddleware)
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom secret")

    return app


async def test_access_log_emits_500_on_unhandled_error(
    make_client: Callable[..., AsyncClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("access-log-500", "dev")

    async with make_client(_build_raising_app()) as client:
        response = await client.get("/boom", headers={"X-Correlation-ID": "boom-corr"})

    assert response.status_code == 500
    records = parse_json_lines(capsys.readouterr().out)
    record = next(item for item in records if item.get("event") == "http_request")
    assert record["status_code"] == 500
    assert record["correlation_id"] == "boom-corr"
    assert record["path"] == "/boom"
