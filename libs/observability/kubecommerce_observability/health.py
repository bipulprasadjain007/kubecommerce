"""Health, startup and readiness endpoint helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from fastapi import APIRouter
from fastapi.responses import JSONResponse

ReadinessCheck = tuple[str, Callable[[], Awaitable[bool]]]

_CHECKS_TIMEOUT_SECONDS = 2.0


async def _run_check(check: Callable[[], Awaitable[bool]]) -> bool:
    """Run a single readiness check, returning False on error or timeout."""
    try:
        result = await asyncio.wait_for(check(), timeout=_CHECKS_TIMEOUT_SECONDS)
    except Exception:
        return False
    return bool(result)


async def _evaluate_checks(
    checks: Sequence[ReadinessCheck],
) -> tuple[bool, dict[str, str]]:
    """Run checks concurrently and return ``(all_ok, per_check_status)``."""
    names = [name for name, _ in checks]
    results = await asyncio.gather(*(_run_check(check) for _, check in checks))
    checks_body = {
        name: ("ok" if passed else "failed") for name, passed in zip(names, results, strict=True)
    }
    return all(results), checks_body


def _checks_response(
    service_name: str,
    version: str,
    all_ok: bool,
    checks_body: dict[str, str],
) -> JSONResponse:
    """Build the readiness/startup JSON response (200 when all checks pass)."""
    body = {
        "status": "ok" if all_ok else "unavailable",
        "service": service_name,
        "version": version,
        "checks": checks_body,
    }
    return JSONResponse(content=body, status_code=200 if all_ok else 503)


def build_health_router(
    service_name: str,
    version: str,
    checks: Sequence[ReadinessCheck] = (),
    *,
    startup_checks: Sequence[ReadinessCheck] = (),
) -> APIRouter:
    """Build the standard liveness, startup and readiness router for a service.

    ``/health/live`` is always 200. ``/health/startup`` and ``/health/ready``
    run their checks concurrently with a per-check timeout and return 503 when
    any check fails; exception text is never included in the response.
    """
    router = APIRouter()

    @router.get("/health/live", include_in_schema=False)
    async def health_live() -> dict[str, str]:
        return {"status": "ok", "service": service_name, "version": version}

    @router.get("/health/startup", include_in_schema=False)
    async def health_startup() -> JSONResponse:
        if not startup_checks:
            return JSONResponse(
                content={"status": "ok", "service": service_name, "version": version}
            )
        all_ok, checks_body = await _evaluate_checks(startup_checks)
        return _checks_response(service_name, version, all_ok, checks_body)

    @router.get("/health/ready", include_in_schema=False)
    async def health_ready() -> JSONResponse:
        all_ok, checks_body = await _evaluate_checks(checks)
        return _checks_response(service_name, version, all_ok, checks_body)

    return router
