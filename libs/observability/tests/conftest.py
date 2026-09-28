"""Shared fixtures for the observability library tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def make_client() -> Callable[..., AsyncClient]:
    """Return a factory that builds an httpx client talking to an ASGI app."""

    def _make(app: FastAPI, *, raise_app_exceptions: bool = False) -> AsyncClient:
        transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        return AsyncClient(transport=transport, base_url="http://testserver")

    return _make


def parse_json_lines(text: str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON log output into a list of records."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]
