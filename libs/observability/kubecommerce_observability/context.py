"""Request-scoped correlation and trace context helpers."""

from __future__ import annotations

import contextvars
import uuid

CORRELATION_ID_HEADER = "X-Correlation-ID"

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)
_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)


def set_correlation_id(value: str | None) -> None:
    """Set the correlation id for the current context."""
    _correlation_id.set(value)


def get_correlation_id() -> str | None:
    """Return the correlation id for the current context, if any."""
    return _correlation_id.get()


def set_trace_id(value: str | None) -> None:
    """Set the fallback trace id for the current context."""
    _trace_id.set(value)


def _active_otel_trace_id() -> str | None:
    """Return the active OpenTelemetry trace id as 32 lowercase hex chars."""
    from opentelemetry import trace

    span_context = trace.get_current_span().get_span_context()
    if span_context is not None and span_context.is_valid:
        return format(span_context.trace_id, "032x")
    return None


def get_trace_id() -> str | None:
    """Return the trace id, preferring the active OpenTelemetry span when present."""
    return _active_otel_trace_id() or _trace_id.get()


def correlation_headers() -> dict[str, str]:
    """Return the ``X-Correlation-ID`` header for outbound propagation.

    Uses the current context value, or generates and stores a new ``uuid4``
    hex id when none is set.
    """
    correlation_id = get_correlation_id()
    if correlation_id is None:
        correlation_id = uuid.uuid4().hex
        set_correlation_id(correlation_id)
    return {CORRELATION_ID_HEADER: correlation_id}


def reset_context() -> None:
    """Clear correlation and trace context for the current context."""
    _correlation_id.set(None)
    _trace_id.set(None)
