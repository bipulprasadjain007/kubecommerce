# ruff: noqa: S603, S607

"""Shared fixtures and HTTP helpers for the KubeCommerce integration suite.

Configuration is entirely environment driven so the same suite runs against a
local compose stack, CI, or a manually started set of processes:

===========================  ==========================================
``GATEWAY_URL``              public gateway (default ``:8080``)
``CATALOG_URL``              catalog-service host port (default ``:8002``)
``WORKER_URL``               notification-worker host port (default ``:8004``)
``WEBHOOK_SINK_URL``         webhook sink (default ``:9000``)
``AUTH_URL``                 auth-service host port (default ``:8001``)
``ORDER_URL``                order-service host port (default ``:8003``)
``RABBITMQ_URL``             broker used by the duplicate-event test
``INTERNAL_API_TOKEN``       shared ``X-Internal-Token`` secret
``RUN_FAILURE_TESTS``        set to ``1`` to enable destructive failure tests
===========================  ==========================================

The suite collects cleanly with no stack present: integration tests skip via
``require_stack`` and failure tests additionally require ``RUN_FAILURE_TESTS=1``
plus a usable Docker CLI. Tests never sleep blindly for long periods; they poll
with a deadline via :func:`wait_for`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

# --- configuration ----------------------------------------------------------


def _first_env(*names: str, default: str) -> str:
    """Return the first non-empty environment variable from ``names``."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080").rstrip("/")
CATALOG_URL = os.environ.get("CATALOG_URL", "http://localhost:8002").rstrip("/")
WORKER_URL = os.environ.get("WORKER_URL", "http://localhost:8004").rstrip("/")
WEBHOOK_SINK_URL = os.environ.get("WEBHOOK_SINK_URL", "http://localhost:9000").rstrip("/")
AUTH_URL = os.environ.get("AUTH_URL", "http://localhost:8001").rstrip("/")
ORDER_URL = os.environ.get("ORDER_URL", "http://localhost:8003").rstrip("/")
# Prefer an explicit broker URL; otherwise mirror the compose RabbitMQ defaults.
RABBITMQ_URL = _first_env(
    "RABBITMQ_URL",
    default=(
        "amqp://"
        f"{os.environ.get('RABBITMQ_DEFAULT_USER', 'kubecommerce')}:"
        f"{os.environ.get('RABBITMQ_DEFAULT_PASS', 'devpassword')}"
        "@localhost:5672/"
    ),
)
# The compose stack uses per-target tokens (all equal in dev); accepting the
# prefixed names prevents a silent 401 when one of them is overridden in .env.
INTERNAL_API_TOKEN = _first_env(
    "INTERNAL_API_TOKEN",
    "GATEWAY_INTERNAL_API_TOKEN",
    "ORDERS_INTERNAL_API_TOKEN",
    "CATALOG_INTERNAL_API_TOKEN",
    default="dev-internal-token-change-me",
)

_TRUTHY = {"1", "true", "yes", "on"}
RUN_FAILURE_TESTS = os.environ.get("RUN_FAILURE_TESTS", "").strip().lower() in _TRUTHY

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = Path(os.environ.get("COMPOSE_FILE", str(REPO_ROOT / "compose.yaml")))

DEFAULT_PASSWORD = "integration-password-123"

#: Service -> readiness endpoint that proves it recovered (best effort).
_HTTP_READY: dict[str, str] = {
    "gateway-api": f"{GATEWAY_URL}/health/ready",
    "auth-service": f"{AUTH_URL}/health/ready",
    "postgres": f"{AUTH_URL}/health/ready",
    "catalog-service": f"{CATALOG_URL}/health/ready",
    "order-service": f"{ORDER_URL}/health/ready",
    "notification-worker": f"{WORKER_URL}/health/ready",
    "rabbitmq": f"{WORKER_URL}/health/ready",
}


# --- generic helpers --------------------------------------------------------


def unique_email(prefix: str = "it") -> str:
    """Return a unique, obviously-synthetic email address."""
    return f"{prefix}-{uuid.uuid4().hex}@example.com"


def unique_sku(prefix: str = "IT") -> str:
    """Return a unique SKU (max 64 characters)."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


async def wait_for(
    predicate: Callable[[], Any] | Callable[[], Awaitable[Any]],
    timeout: float = 30.0,  # noqa: ASYNC109 - polling helper, not asyncio.timeout
    interval: float = 0.5,
) -> Any:
    """Poll ``predicate`` until it returns a truthy value or ``timeout`` elapses.

    ``predicate`` may be sync or async; exceptions are treated as "not yet"
    (transient connection errors while a service restarts are expected). The
    truthy value is returned so callers can reuse it.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        try:
            result = predicate()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            last_error = exc
            result = None
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"condition not met within {timeout:.1f}s") from last_error
        await asyncio.sleep(interval)


# --- HTTP helpers -----------------------------------------------------------


async def register_user(
    client: httpx.AsyncClient,
    *,
    email: str | None = None,
    password: str = DEFAULT_PASSWORD,
    full_name: str | None = None,
) -> dict[str, Any]:
    """Register a user through the gateway and return the API response body."""
    payload: dict[str, Any] = {"email": email or unique_email(), "password": password}
    if full_name is not None:
        payload["full_name"] = full_name
    response = await client.post(f"{GATEWAY_URL}/api/auth/users", json=payload)
    response.raise_for_status()
    return response.json()


async def login(
    client: httpx.AsyncClient,
    email: str,
    password: str = DEFAULT_PASSWORD,
) -> str:
    """Authenticate through the gateway and return the bearer access token."""
    response = await client.post(
        f"{GATEWAY_URL}/api/auth/login",
        json={"email": email, "password": password},
    )
    response.raise_for_status()
    return response.json()["access_token"]


def auth_headers(token: str) -> dict[str, str]:
    """Return the bearer ``Authorization`` header for ``token``."""
    return {"Authorization": f"Bearer {token}"}


async def create_product(
    client: httpx.AsyncClient,
    *,
    sku: str | None = None,
    name: str = "Integration Product",
    price_cents: int = 1000,
    stock: int = 100,
) -> dict[str, Any]:
    """Create a product directly in catalog-service using the internal token."""
    response = await client.post(
        f"{CATALOG_URL}/products",
        json={
            "sku": sku or unique_sku(),
            "name": name,
            "description": "created by integration tests",
            "price_cents": price_cents,
            "stock": stock,
        },
        headers={"X-Internal-Token": INTERNAL_API_TOKEN},
    )
    response.raise_for_status()
    return response.json()


async def create_order(
    client: httpx.AsyncClient,
    token: str,
    items: list[dict[str, Any]],
    *,
    idempotency_key: str | None = None,
) -> httpx.Response:
    """Create an order through the gateway; returns the raw response."""
    headers = auth_headers(token)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return await client.post(f"{GATEWAY_URL}/api/orders", json={"items": items}, headers=headers)


async def get_order(client: httpx.AsyncClient, token: str, order_id: str) -> httpx.Response:
    """Fetch a single order through the gateway."""
    return await client.get(f"{GATEWAY_URL}/api/orders/{order_id}", headers=auth_headers(token))


async def list_orders(client: httpx.AsyncClient, token: str) -> httpx.Response:
    """List the caller's orders through the gateway."""
    return await client.get(f"{GATEWAY_URL}/api/orders", headers=auth_headers(token))


async def clear_events(client: httpx.AsyncClient) -> None:
    """Clear every event stored in the webhook sink."""
    response = await client.delete(f"{WEBHOOK_SINK_URL}/events")
    response.raise_for_status()


async def get_events(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Return every event currently stored in the webhook sink."""
    response = await client.get(f"{WEBHOOK_SINK_URL}/events")
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


async def publish_order_created_event(
    body: Mapping[str, Any],
    headers: Mapping[str, Any],
    *,
    rabbitmq_url: str | None = None,
    routing_key: str = "order.created",
    exchange_name: str = "kubecommerce.events",
) -> None:
    """Publish one persistent event to the events topic exchange via aio-pika."""
    import aio_pika

    connection = await aio_pika.connect_robust(rabbitmq_url or RABBITMQ_URL)
    try:
        channel = await connection.channel(publisher_confirms=True)
        exchange = await channel.declare_exchange(
            exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
        )
        message = aio_pika.Message(
            body=json.dumps(body).encode("utf-8"),
            headers=dict(headers),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
        )
        await exchange.publish(message, routing_key=routing_key)
    finally:
        await connection.close()


def build_order_created_event(
    *,
    event_id: str | None = None,
    order_id: str | None = None,
    user_id: str | None = None,
    total_cents: int = 1000,
) -> dict[str, Any]:
    """Build a valid ``order.created`` v1 envelope with unique identifiers."""
    now = datetime.now(UTC).isoformat()
    return {
        "event_type": "order.created",
        "event_version": 1,
        "event_id": event_id or str(uuid.uuid4()),
        "occurred_at": now,
        "correlation_id": f"it-{uuid.uuid4().hex}",
        "producer": "integration-tests",
        "data": {
            "order_id": order_id or str(uuid.uuid4()),
            "user_id": user_id or str(uuid.uuid4()),
            "items": [
                {
                    "product_id": str(uuid.uuid4()),
                    "sku": "IT-SKU",
                    "quantity": 1,
                    "unit_price_cents": total_cents,
                }
            ],
            "total_cents": total_cents,
            "currency": "USD",
            "created_at": now,
        },
    }


def event_headers(event: Mapping[str, Any], *, correlation_id: str | None = None) -> dict[str, str]:
    """Build the AMQP headers the contracts layer would emit for ``event``."""
    return {
        "event_id": str(event["event_id"]),
        "event_type": str(event["event_type"]),
        "event_version": str(event["event_version"]),
        "X-Correlation-ID": correlation_id or str(event["correlation_id"]),
        "x-retry-count": "0",
    }


# --- stack / failure gating -------------------------------------------------


@pytest.fixture(scope="session")
def stack_ready() -> bool:
    """Return True when the gateway AND worker report ready (once per session).

    Gateway readiness is dependency-free by design, so the worker readiness
    (broker connection) is probed as well before tests rely on the messaging
    path or on `docker compose stop/start`.
    """
    try:
        with httpx.Client(timeout=3.0) as probe:
            gateway_ok = probe.get(f"{GATEWAY_URL}/health/ready").status_code == 200
            worker_ok = probe.get(f"{WORKER_URL}/health/ready").status_code == 200
        return gateway_ok and worker_ok
    except httpx.HTTPError:
        return False


@pytest.fixture(autouse=True)
def require_stack(request: pytest.FixtureRequest, stack_ready: bool) -> None:
    """Skip integration tests cleanly when no compose stack is running."""
    if request.node.get_closest_marker("integration") and not stack_ready:
        pytest.skip("stack not running")


@pytest.fixture(scope="session")
def docker_available() -> bool:
    """Return True when a ``docker`` CLI is on PATH."""
    import shutil

    return shutil.which("docker") is not None


@pytest.fixture(scope="session")
def run_failure_tests() -> bool:
    """Return True when destructive failure tests were explicitly requested."""
    return RUN_FAILURE_TESTS


@pytest.fixture(autouse=True)
def require_failure_env(
    request: pytest.FixtureRequest,
    docker_available: bool,
    run_failure_tests: bool,
) -> None:
    """Skip failure tests unless the opt-in env and Docker are both present."""
    if not request.node.get_closest_marker("failure"):
        return
    if not run_failure_tests:
        pytest.skip("RUN_FAILURE_TESTS is not enabled")
    if not docker_available:
        pytest.skip("docker is not available")
    if not COMPOSE_FILE.exists():
        pytest.skip(f"compose file not found: {COMPOSE_FILE}")


# --- HTTP client fixture ----------------------------------------------------


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield a shared async HTTP client for the stack under test."""
    async with httpx.AsyncClient(timeout=15.0) as http_client:
        yield http_client


# --- docker compose control (failure tests only) ----------------------------


class ComposeController:
    """Thin wrapper around ``docker compose`` for stop/start failure injection.

    Stopped services are tracked so the fixture teardown can always restore the
    stack even if a test fails before its own ``finally`` block runs. Every
    start waits for the service to become healthy again (HTTP readiness where a
    host port exists, otherwise the container health status).
    """

    def __init__(self, compose_file: Path) -> None:
        self._file = compose_file
        self._stopped: set[str] = set()

    def _run(self, *args: str) -> None:
        subprocess.run(
            ["docker", "compose", "-f", str(self._file), *args],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )

    def _service_healthy(self, service: str) -> bool:
        container = subprocess.run(
            ["docker", "compose", "-f", str(self._file), "ps", "-q", service],
            cwd=str(REPO_ROOT),
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not container:
            return False
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                *container.splitlines(),
            ],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.split()
        return bool(inspected) and all(state in {"healthy", "running"} for state in inspected)

    async def __call__(self, action: str, service: str) -> None:
        """Perform ``stop``/``start``/``restart`` for ``service``."""
        if action == "stop":
            await asyncio.to_thread(self._run, "stop", service)
            self._stopped.add(service)
            return
        if action in {"start", "restart"}:
            if service in self._stopped or action == "restart":
                await asyncio.to_thread(self._run, "start", service)
            self._stopped.discard(service)
            await self.wait_healthy(service)
            return
        raise ValueError(f"unsupported compose action: {action!r}")

    async def wait_healthy(self, service: str, seconds: float = 90.0) -> None:
        """Poll until ``service`` is ready again or ``seconds`` elapses."""
        http_url = _HTTP_READY.get(service)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            healthy = False
            if http_url is not None:
                try:
                    async with httpx.AsyncClient(timeout=3.0) as probe:
                        response = await probe.get(http_url)
                    healthy = response.status_code == 200
                except httpx.HTTPError:
                    healthy = False
            else:
                healthy = await asyncio.to_thread(self._service_healthy, service)
            if healthy:
                return
            await asyncio.sleep(1.0)
        raise TimeoutError(f"{service} did not become healthy within {seconds:.0f}s")

    async def restore(self) -> None:
        """Restart every service we stopped, then wait for health."""
        errors: list[str] = []
        for service in sorted(self._stopped):
            self._stopped.discard(service)
            try:
                await asyncio.to_thread(self._run, "start", service)
                await self.wait_healthy(service)
            except (subprocess.CalledProcessError, TimeoutError) as exc:
                errors.append(f"{service}: {exc}")
        if errors:
            raise RuntimeError("failed to restore services: " + "; ".join(errors))


@pytest.fixture
async def compose() -> AsyncIterator[ComposeController]:
    """Yield a compose controller that guarantees services are restarted."""
    controller = ComposeController(COMPOSE_FILE)
    try:
        yield controller
    finally:
        await controller.restore()
