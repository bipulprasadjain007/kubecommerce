"""Standard API error model and FastAPI exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .context import get_correlation_id, get_trace_id
from .logging import get_logger

logger = get_logger("kubecommerce.errors")

_CORRELATION_HEADER = "X-Correlation-ID"


class ApiError(Exception):
    """Domain error rendered as the standard error envelope.

    ``details`` is serialized into the response body and must only ever carry
    client-safe data - never raw driver, database or upstream error payloads.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def _resolve_correlation_id(request: Request | None) -> str | None:
    """Return the correlation id from context, falling back to request state."""
    correlation_id = get_correlation_id()
    if correlation_id is None and request is not None:
        correlation_id = getattr(request.state, "correlation_id", None)
    return correlation_id


def _correlation_headers(request: Request | None) -> dict[str, str]:
    """Return the correlation id response header when one is available."""
    correlation_id = _resolve_correlation_id(request)
    if correlation_id is None:
        return {}
    return {_CORRELATION_HEADER: correlation_id}


def error_response(
    request: Request | None,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    """Build the standard ``{"error": {...}}`` JSON response."""
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "correlation_id": _resolve_correlation_id(request),
    }
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers=_correlation_headers(request),
    )


def _sanitized_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Return validation errors without URLs, input values or internal context."""
    return [
        {
            "loc": list(error.get("loc", ())),
            "msg": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in exc.errors()
    ]


def register_exception_handlers(app: FastAPI) -> None:
    """Register the standard exception handlers on a FastAPI app."""

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = _sanitized_validation_errors(exc)
        return error_response(
            request,
            422,
            "validation_error",
            "Request validation failed",
            details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error_response(request, exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Exception messages can embed DSNs/tokens, so never log the traceback
        # at ERROR. Correlate via request.state because contextvars have already
        # been reset by CorrelationIdMiddleware at this point.
        correlation_id = _resolve_correlation_id(request)
        bound = logger.bind(
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
            trace_id=get_trace_id(),
        )
        bound.error("unhandled_exception")
        bound.debug("unhandled_exception_detail", exc_info=True)
        return error_response(request, 500, "internal_error", "Internal server error")
