"""HTTP middleware for correlation ids and structured access logging."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Sequence

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from .context import (
    CORRELATION_ID_HEADER,
    get_correlation_id,
    reset_context,
    set_correlation_id,
)
from .logging import get_logger

_CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
DEFAULT_EXCLUDE_PATHS: tuple[str, ...] = (
    "/health/live",
    "/health/ready",
    "/health/startup",
    "/metrics",
)

_access_logger = get_logger("kubecommerce.access")


def _is_valid_correlation_id(value: str) -> bool:
    """Return True when ``value`` is a safe correlation id token."""
    return _CORRELATION_ID_PATTERN.fullmatch(value) is not None


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Ensure every request has a correlation id in context and response headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(CORRELATION_ID_HEADER)
        if incoming is not None and _is_valid_correlation_id(incoming):
            correlation_id = incoming
        else:
            correlation_id = uuid.uuid4().hex

        set_correlation_id(correlation_id)
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        finally:
            reset_context()
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emit one structured access log per request, including failed requests."""

    def __init__(
        self,
        app: ASGIApp,
        service_name: str,
        exclude_paths: Sequence[str] = DEFAULT_EXCLUDE_PATHS,
    ) -> None:
        super().__init__(app)
        self.service_name = service_name
        self.exclude_paths = frozenset(exclude_paths)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            # Runs for both success and failure so 500s are still access-logged.
            self._emit_access_log(request, status_code, started)

    def _emit_access_log(self, request: Request, status_code: int, started: float) -> None:
        """Write the ``http_request`` record unless the path is excluded."""
        if request.url.path in self.exclude_paths:
            return

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            path = "unmatched"

        # Fall back to request.state for failures that unwind past the
        # correlation middleware's context reset.
        correlation_id = get_correlation_id() or getattr(request.state, "correlation_id", None)
        _access_logger.info(
            "http_request",
            method=request.method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
            service=self.service_name,
        )
