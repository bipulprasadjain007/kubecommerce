"""Tests for structured JSON logging configuration."""

from __future__ import annotations

import logging as stdlib_logging

import pytest
from conftest import parse_json_lines

from kubecommerce_observability.context import reset_context, set_correlation_id
from kubecommerce_observability.logging import configure_logging, get_logger


def test_structlog_emits_json_with_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("logging-test", "dev", level="INFO")
    set_correlation_id("corr-1")
    get_logger("test.logger").info("hello", custom="value")
    reset_context()

    captured = capsys.readouterr()
    records = parse_json_lines(captured.out + captured.err)
    record = next(item for item in records if item.get("event") == "hello")

    assert record["service"] == "logging-test"
    assert record["environment"] == "dev"
    assert record["level"] == "info"
    assert record["custom"] == "value"
    assert record["correlation_id"] == "corr-1"
    assert isinstance(record["timestamp"], str)
    assert record["timestamp"]


def test_stdlib_logging_is_routed_through_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("logging-test-stdlib", "staging", level="INFO")
    stdlib_logging.getLogger("some.uvicorn").warning("root message")

    captured = capsys.readouterr()
    records = parse_json_lines(captured.out + captured.err)
    record = next(item for item in records if item.get("event") == "root message")

    assert record["level"] == "warning"
    assert record["service"] == "logging-test-stdlib"
    assert record["environment"] == "staging"


def test_configure_logging_is_idempotent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("logging-test-idempotent", "dev")
    configure_logging("logging-test-idempotent", "dev")
    get_logger().info("only-once")

    captured = capsys.readouterr()
    records = parse_json_lines(captured.out + captured.err)
    assert sum(1 for item in records if item.get("event") == "only-once") == 1


def test_log_level_is_applied(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("logging-test-level", "dev", level="WARNING")
    logger = get_logger("level.test")
    logger.info("should-be-filtered")
    logger.warning("should-appear")

    captured = capsys.readouterr()
    records = parse_json_lines(captured.out + captured.err)
    events = {item.get("event") for item in records}
    assert "should-appear" in events
    assert "should-be-filtered" not in events


def test_uvicorn_loggers_propagate_through_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("uvicorn-propagation", "dev")

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = stdlib_logging.getLogger(name)
        assert uvicorn_logger.propagate is True
        assert uvicorn_logger.handlers == []

    stdlib_logging.getLogger("uvicorn.error").error("startup failure")

    records = parse_json_lines(capsys.readouterr().out)
    record = next(item for item in records if item.get("event") == "startup failure")
    assert record["level"] == "error"
    assert record["service"] == "uvicorn-propagation"
