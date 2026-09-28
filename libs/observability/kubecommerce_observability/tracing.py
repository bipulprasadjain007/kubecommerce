"""OpenTelemetry tracing configuration helpers."""

from __future__ import annotations

import weakref
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Tracer

_TRACING_CONFIGURED = False
_TRACING_PROVIDER: Any = None
_INSTRUMENTED_APPS: weakref.WeakSet[Any] = weakref.WeakSet()


def _traces_endpoint(otlp_endpoint: str) -> str:
    """Return the OTLP HTTP traces URL for a base collector endpoint."""
    base = otlp_endpoint.rstrip("/")
    if base.endswith("/v1/traces"):
        return base
    return f"{base}/v1/traces"


def configure_tracing(
    service_name: str,
    environment: str,
    service_version: str = "0.0.0",
    otlp_endpoint: str | None = None,
    enabled: bool = False,
    sample_ratio: float = 1.0,
) -> None:
    """Configure the global tracer provider when tracing is enabled.

    Installs a ``ParentBased(TraceIdRatioBased)`` sampler, an OTLP HTTP batch
    exporter and the HTTPX client instrumentation. Does nothing when ``enabled``
    is False or tracing was already configured.
    """
    global _TRACING_CONFIGURED, _TRACING_PROVIDER
    if not enabled or _TRACING_CONFIGURED:
        return

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment": environment,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(root=TraceIdRatioBased(sample_ratio)),
    )
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=_traces_endpoint(otlp_endpoint))
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    _TRACING_PROVIDER = provider
    _TRACING_CONFIGURED = True


def shutdown_tracing() -> None:
    """Force-flush and shut down the configured tracer provider.

    Idempotent and a safe no-op when tracing was never enabled.
    """
    global _TRACING_CONFIGURED, _TRACING_PROVIDER
    provider = _TRACING_PROVIDER
    if provider is None:
        return
    try:
        provider.force_flush()
    finally:
        provider.shutdown()
        _TRACING_PROVIDER = None
        _TRACING_CONFIGURED = False


def instrument_fastapi(app: Any, service_name: str) -> None:
    """Instrument a FastAPI app for tracing, excluding health and metrics URLs.

    No-op unless tracing has been configured, and never instruments the same
    app instance twice.
    """
    if not _TRACING_CONFIGURED:
        return
    if app in _INSTRUMENTED_APPS:
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    _INSTRUMENTED_APPS.add(app)


def get_tracer(name: str) -> Tracer:
    """Return an OpenTelemetry tracer for ``name``."""
    return trace.get_tracer(name)
