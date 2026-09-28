"""Tests for OpenTelemetry tracing configuration helpers."""

from __future__ import annotations

import weakref

import pytest
from fastapi import FastAPI

from kubecommerce_observability import tracing
from kubecommerce_observability.tracing import (
    _traces_endpoint,
    configure_tracing,
    get_tracer,
    instrument_fastapi,
    shutdown_tracing,
)


@pytest.fixture(autouse=True)
def _reset_tracing_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-level tracing guards between tests."""
    monkeypatch.setattr(tracing, "_TRACING_CONFIGURED", False)
    monkeypatch.setattr(tracing, "_TRACING_PROVIDER", None)
    monkeypatch.setattr(tracing, "_INSTRUMENTED_APPS", weakref.WeakSet())


def _patch_instrumentors(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
    import opentelemetry.instrumentation.fastapi as fastapi_instrumentation
    import opentelemetry.instrumentation.httpx as httpx_instrumentation

    calls: dict[str, list[object]] = {"http": [], "fastapi": []}
    monkeypatch.setattr(
        httpx_instrumentation.HTTPXClientInstrumentor,
        "instrument",
        lambda self, **kwargs: calls["http"].append(self),
    )
    monkeypatch.setattr(
        fastapi_instrumentation.FastAPIInstrumentor,
        "instrument_app",
        lambda app, **kwargs: calls["fastapi"].append(app),
    )
    return calls


def test_traces_endpoint_appends_path() -> None:
    assert _traces_endpoint("http://otel-collector:4318") == (
        "http://otel-collector:4318/v1/traces"
    )
    assert _traces_endpoint("http://otel-collector:4318/") == (
        "http://otel-collector:4318/v1/traces"
    )
    assert _traces_endpoint("http://otel-collector:4318/v1/traces") == (
        "http://otel-collector:4318/v1/traces"
    )


def test_configure_tracing_disabled_is_noop() -> None:
    configure_tracing("trace-svc", "dev", enabled=False)
    assert tracing._TRACING_CONFIGURED is False
    assert tracing._TRACING_PROVIDER is None
    assert get_tracer("noop") is not None


def test_configure_tracing_enabled_builds_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry import trace

    providers: list[object] = []
    monkeypatch.setattr(trace, "set_tracer_provider", providers.append)
    calls = _patch_instrumentors(monkeypatch)

    configure_tracing(
        "trace-svc",
        "staging",
        service_version="1.0.0",
        otlp_endpoint="http://localhost:4318",
        enabled=True,
        sample_ratio=0.5,
    )
    # Second call must be a no-op due to the idempotency guard.
    configure_tracing("trace-svc", "staging", enabled=True)

    assert len(providers) == 1
    assert len(calls["http"]) == 1
    assert tracing._TRACING_CONFIGURED is True
    assert tracing._TRACING_PROVIDER is not None

    shutdown_tracing()
    # Idempotent: a second call is a safe no-op.
    shutdown_tracing()
    assert tracing._TRACING_PROVIDER is None
    assert tracing._TRACING_CONFIGURED is False


def test_shutdown_tracing_is_noop_when_disabled() -> None:
    shutdown_tracing()
    shutdown_tracing()
    assert tracing._TRACING_PROVIDER is None
    assert tracing._TRACING_CONFIGURED is False


def test_instrument_fastapi_noop_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_instrumentors(monkeypatch)
    app = FastAPI()
    instrument_fastapi(app, "trace-svc")
    assert calls["fastapi"] == []


def test_instrument_fastapi_does_not_double_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_instrumentors(monkeypatch)
    monkeypatch.setattr(tracing, "_TRACING_CONFIGURED", True)
    app = FastAPI()

    instrument_fastapi(app, "trace-svc")
    instrument_fastapi(app, "trace-svc")

    assert calls["fastapi"] == [app]
    assert get_tracer("enabled") is not None
