"""Structured JSON logging configuration built on structlog.

The configuration routes both structlog events and standard library logging
(uvicorn, third-party packages, the root logger) through a single JSON
formatter writing ISO-8601 UTC records to stdout.
"""

from __future__ import annotations

import logging as stdlib_logging
import sys
from typing import Any, cast

import structlog
from structlog.stdlib import BoundLogger
from structlog.typing import EventDict, Processor

from .context import get_correlation_id, get_trace_id

_CONFIGURED_SIGNATURE: tuple[str, str, str] | None = None

#: Uvicorn loggers configured to propagate to the root structlog handler.
_UVICORN_LOGGERS: tuple[str, ...] = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _add_observability_context(service_name: str, environment: str) -> Processor:
    """Build a processor that injects service/context fields into every event."""

    def processor(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service_name)
        event_dict.setdefault("environment", environment)
        event_dict.setdefault("correlation_id", get_correlation_id())
        event_dict.setdefault("trace_id", get_trace_id())
        return event_dict

    return processor


def _shared_processors(service_name: str, environment: str) -> list[Processor]:
    """Return the processors shared by structlog events and foreign records."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _add_observability_context(service_name, environment),
    ]


def configure_logging(service_name: str, environment: str, level: str = "INFO") -> None:
    """Configure JSON logging for a service.

    Idempotent for identical arguments. Standard library logging is routed
    through the same processor formatter so uvicorn and root log records are
    emitted as JSON too.
    """
    global _CONFIGURED_SIGNATURE

    signature = (service_name, environment, level.upper())
    if signature == _CONFIGURED_SIGNATURE:
        return

    log_level = stdlib_logging.getLevelNamesMapping().get(level.upper(), stdlib_logging.INFO)

    shared = _shared_processors(service_name, environment)
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = stdlib_logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = stdlib_logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(log_level)

    # uvicorn installs its own plain-text handlers and sets propagate=False.
    # Clear them so startup/access/error logs flow through the JSON pipeline.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = stdlib_logging.getLogger(name)
        for existing in list(uvicorn_logger.handlers):
            uvicorn_logger.removeHandler(existing)
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(log_level)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    _CONFIGURED_SIGNATURE = signature


def get_logger(name: str | None = None) -> BoundLogger:
    """Return a structlog bound logger, optionally namespaced by ``name``."""
    logger = structlog.get_logger(name) if name is not None else structlog.get_logger()
    return cast(BoundLogger, logger)
