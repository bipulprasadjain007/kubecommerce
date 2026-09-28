"""Tests for correlation and trace context helpers."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from kubecommerce_observability.context import (
    get_correlation_id,
    get_trace_id,
    reset_context,
    set_correlation_id,
    set_trace_id,
)


def test_correlation_id_roundtrip() -> None:
    reset_context()
    assert get_correlation_id() is None
    set_correlation_id("abc-123")
    assert get_correlation_id() == "abc-123"
    set_correlation_id(None)
    assert get_correlation_id() is None


def test_trace_id_contextvar_fallback() -> None:
    reset_context()
    set_trace_id("trace-from-contextvar")
    assert get_trace_id() == "trace-from-contextvar"


def test_trace_id_prefers_active_span() -> None:
    reset_context()
    set_trace_id("fallback")
    span_context = SpanContext(
        trace_id=0x1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    with trace.use_span(NonRecordingSpan(span_context), end_on_exit=False):
        assert get_trace_id() == format(0x1234567890ABCDEF, "032x")


def test_reset_context_clears_values() -> None:
    set_correlation_id("x")
    set_trace_id("y")
    reset_context()
    assert get_correlation_id() is None
    assert get_trace_id() is None
