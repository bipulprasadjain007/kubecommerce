"""Tests for health/readiness endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import FastAPI
from httpx import AsyncClient

from kubecommerce_observability import health as health_module
from kubecommerce_observability.health import ReadinessCheck, build_health_router


def _build_app(checks: tuple[ReadinessCheck, ...] = ()) -> FastAPI:
    app = FastAPI()
    app.include_router(build_health_router("svc", "1.2.3", checks))
    return app


def _build_startup_app(checks: tuple[ReadinessCheck, ...] = ()) -> FastAPI:
    app = FastAPI()
    app.include_router(build_health_router("svc", "1.2.3", startup_checks=checks))
    return app


async def test_live_and_startup_are_ok(make_client: Callable[..., AsyncClient]) -> None:
    async with make_client(_build_app()) as client:
        live = await client.get("/health/live")
        startup = await client.get("/health/startup")

    assert live.status_code == 200
    assert live.json() == {"status": "ok", "service": "svc", "version": "1.2.3"}
    assert startup.status_code == 200
    assert startup.json() == {"status": "ok", "service": "svc", "version": "1.2.3"}


async def test_ready_without_checks_is_ok(make_client: Callable[..., AsyncClient]) -> None:
    async with make_client(_build_app()) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "svc",
        "version": "1.2.3",
        "checks": {},
    }


async def test_ready_with_passing_check(make_client: Callable[..., AsyncClient]) -> None:
    async def ok() -> bool:
        return True

    async with make_client(_build_app((("database", ok),))) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok"}


async def test_ready_with_failing_check(make_client: Callable[..., AsyncClient]) -> None:
    async def broken() -> bool:
        return False

    async with make_client(_build_app((("cache", broken),))) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["checks"] == {"cache": "failed"}


async def test_ready_with_raising_check_does_not_leak(
    make_client: Callable[..., AsyncClient],
) -> None:
    async def exploding() -> bool:
        raise RuntimeError("secret connection string")

    async with make_client(_build_app((("broker", exploding),))) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"broker": "failed"}
    assert "secret connection string" not in response.text


async def test_ready_with_timeout(
    make_client: Callable[..., AsyncClient],
    monkeypatch: object,
) -> None:
    async def slow() -> bool:
        await asyncio.sleep(1)
        return True

    assert hasattr(monkeypatch, "setattr")
    monkeypatch.setattr(health_module, "_CHECKS_TIMEOUT_SECONDS", 0.05)  # type: ignore[attr-defined]

    async with make_client(_build_app((("slow", slow),))) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"slow": "failed"}


async def test_ready_runs_checks_concurrently(
    make_client: Callable[..., AsyncClient],
) -> None:
    started: list[str] = []

    async def first() -> bool:
        started.append("first")
        await asyncio.sleep(0.05)
        return True

    async def second() -> bool:
        started.append("second")
        await asyncio.sleep(0.05)
        return True

    async with make_client(_build_app((("a", first), ("b", second)))) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert set(started) == {"first", "second"}


async def test_startup_defaults_to_ok(make_client: Callable[..., AsyncClient]) -> None:
    async with make_client(_build_startup_app()) as client:
        response = await client.get("/health/startup")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "svc", "version": "1.2.3"}


async def test_startup_with_passing_check(make_client: Callable[..., AsyncClient]) -> None:
    async def ok() -> bool:
        return True

    async with make_client(_build_startup_app((("migrations", ok),))) as client:
        response = await client.get("/health/startup")

    assert response.status_code == 200
    assert response.json()["checks"] == {"migrations": "ok"}


async def test_startup_with_failing_check(make_client: Callable[..., AsyncClient]) -> None:
    async def broken() -> bool:
        return False

    async with make_client(_build_startup_app((("migrations", broken),))) as client:
        response = await client.get("/health/startup")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["checks"] == {"migrations": "failed"}


async def test_startup_raising_check_does_not_leak(
    make_client: Callable[..., AsyncClient],
) -> None:
    async def exploding() -> bool:
        raise RuntimeError("postgresql://user:pass@host/db")

    async with make_client(_build_startup_app((("migrations", exploding),))) as client:
        response = await client.get("/health/startup")

    assert response.status_code == 503
    assert response.json()["checks"] == {"migrations": "failed"}
    assert "postgresql://user:pass@host/db" not in response.text
