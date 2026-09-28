"""Tests for the one-call observability bootstrap."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import parse_json_lines
from fastapi import FastAPI
from httpx import AsyncClient

from kubecommerce_observability import bootstrap
from kubecommerce_observability.bootstrap import install_observability
from kubecommerce_observability.metrics import ServiceMetrics
from kubecommerce_observability.settings import BaseServiceSettings


async def _ok() -> bool:
    return True


def _settings(**overrides: object) -> BaseServiceSettings:
    values: dict[str, object] = {
        "service_name": "bootstrap-svc",
        "environment": "dev",
        "log_level": "INFO",
        "service_version": "3.2.1",
        "otel_enabled": False,
        "_env_file": None,
    }
    values.update(overrides)
    return BaseServiceSettings(**values)  # type: ignore[arg-type]


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/echo/{value}")
    async def echo(value: str) -> dict[str, str]:
        return {"value": value}

    return app


async def test_install_observability_wires_everything(
    make_client: Callable[..., AsyncClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = _build_app()
    metrics = install_observability(
        app,
        settings=_settings(),
        readiness_checks=(("db", _ok),),
        startup_checks=(("migrations", _ok),),
    )

    assert isinstance(metrics, ServiceMetrics)
    assert metrics.service_name == "bootstrap-svc"

    async with make_client(app) as client:
        echo = await client.get("/echo/abc-123", headers={"X-Correlation-ID": "boot-corr"})
        metrics_response = await client.get("/metrics")
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        startup = await client.get("/health/startup")

    assert echo.status_code == 200
    assert metrics_response.status_code == 200
    assert "kubecommerce_http_requests_total" in metrics_response.text
    assert 'service="bootstrap-svc"' in metrics_response.text
    assert live.status_code == 200
    assert ready.status_code == 200
    assert startup.status_code == 200

    records = parse_json_lines(capsys.readouterr().out)
    access = next(
        item
        for item in records
        if item.get("event") == "http_request" and item.get("path") == "/echo/{value}"
    )
    # The correlation id proves AccessLog ran inside CorrelationId middleware.
    assert access["correlation_id"] == "boot-corr"
    assert access["service"] == "bootstrap-svc"
    assert access["status_code"] == 200


def test_install_observability_enables_tracing_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracing_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    instrument_calls: list[tuple[FastAPI, str]] = []
    monkeypatch.setattr(
        bootstrap,
        "configure_tracing",
        lambda *args, **kwargs: tracing_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        bootstrap,
        "instrument_fastapi",
        lambda app, name: instrument_calls.append((app, name)),
    )

    app = FastAPI()
    metrics = install_observability(
        app,
        settings=_settings(
            service_name="otel-svc",
            environment="staging",
            otel_enabled=True,
            otel_exporter_otlp_endpoint="http://otel:4318",
            otel_sample_ratio=0.5,
        ),
    )

    assert metrics.service_name == "otel-svc"
    assert len(tracing_calls) == 1
    args, kwargs = tracing_calls[0]
    assert args == ("otel-svc", "staging")
    assert kwargs["service_version"] == "3.2.1"
    assert kwargs["otlp_endpoint"] == "http://otel:4318"
    assert kwargs["enabled"] is True
    assert kwargs["sample_ratio"] == 0.5
    assert instrument_calls == [(app, "otel-svc")]


def test_install_observability_skips_tracing_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[object] = []
    monkeypatch.setattr(bootstrap, "configure_tracing", lambda *a, **k: called.append("tracing"))
    monkeypatch.setattr(bootstrap, "instrument_fastapi", lambda app, name: called.append("instr"))

    install_observability(FastAPI(), settings=_settings(otel_enabled=False))
    assert called == []
