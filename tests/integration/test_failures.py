"""Controlled failure tests for KubeCommerce (destructive; opt-in).

Every test here stops a compose service, asserts the documented degradation
behaviour, and restarts it in a ``finally`` block (the ``compose`` fixture also
restores anything still stopped, so the stack is left healthy even on failure).
Run with::

    RUN_FAILURE_TESTS=1 uv run pytest -q -m failure

Out of scope: a *precise downstream timeout* test (catalog slower than
``ORDERS_REQUEST_TIMEOUT_SECONDS``) needs deterministic network latency
injection such as toxiproxy; that is deferred rather than approximated with a
flaky sleep. The catalog-down test below covers unreachability, and the broker
outage is covered via a full stop.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from conftest import (
    AUTH_URL,
    CATALOG_URL,
    GATEWAY_URL,
    build_order_created_event,
    clear_events,
    create_order,
    create_product,
    event_headers,
    get_events,
    list_orders,
    login,
    publish_order_created_event,
    register_user,
    unique_email,
    unique_sku,
    wait_for,
)

pytestmark = [pytest.mark.integration, pytest.mark.failure]


async def _provision(client: Any, *, price_cents: int = 1000, stock: int = 50) -> tuple[str, dict]:
    """Create a user and a product; return the bearer token and product."""
    email = unique_email("failure")
    await register_user(client, email=email)
    token = await login(client, email)
    product = await create_product(
        client, sku=unique_sku("FAIL"), price_cents=price_cents, stock=stock
    )
    return token, product


async def test_redis_outage_is_fail_open(client: Any, compose: Any) -> None:
    """Catalog reads still succeed while Redis is down (cache fail-open)."""
    _, product = await _provision(client)

    await compose("stop", "redis")
    try:
        direct = await client.get(f"{CATALOG_URL}/products/{product['id']}")
        assert direct.status_code == 200, direct.text
        assert direct.json()["id"] == product["id"]

        through_gateway = await client.get(f"{GATEWAY_URL}/api/catalog/products/{product['id']}")
        assert through_gateway.status_code == 200, through_gateway.text
    finally:
        await compose("start", "redis")


async def test_rabbitmq_outage_order_survives_via_outbox(client: Any, compose: Any) -> None:
    """``POST /orders`` succeeds while the broker is down; the event ships later."""
    token, product = await _provision(client)
    await clear_events(client)

    await compose("stop", "rabbitmq")
    try:
        created = await create_order(client, token, [{"product_id": product["id"], "quantity": 1}])
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]
    finally:
        await compose("start", "rabbitmq")

    async def _delivered() -> list[dict[str, Any]]:
        events = await get_events(client)
        return [event for event in events if event.get("order_id") == order_id]

    matches = await wait_for(_delivered, timeout=60.0, interval=1.0)
    assert len(matches) == 1
    assert matches[0]["order_id"] == order_id


async def test_postgres_outage_degrades_auth_but_not_gateway(client: Any, compose: Any) -> None:
    """Auth readiness fails with the DB down while the gateway stays ready."""
    await compose("stop", "postgres")
    try:
        auth_ready = await client.get(f"{AUTH_URL}/health/ready")
        assert auth_ready.status_code == 503, auth_ready.text

        gateway_ready = await client.get(f"{GATEWAY_URL}/health/ready")
        assert gateway_ready.status_code == 200, gateway_ready.text
    finally:
        await compose("start", "postgres")

    recovered = await client.get(f"{AUTH_URL}/health/ready")
    assert recovered.status_code == 200, recovered.text


async def test_catalog_outage_fails_order_without_persisting(client: Any, compose: Any) -> None:
    """Order creation returns 502 when catalog is down and creates no order."""
    token, product = await _provision(client)

    await compose("stop", "catalog-service")
    try:
        response = await create_order(client, token, [{"product_id": product["id"], "quantity": 1}])
        assert response.status_code == 502, response.text
        code = response.json().get("error", {}).get("code")
        assert code in {"catalog_unavailable", "upstream_unavailable"}
    finally:
        await compose("start", "catalog-service")

    listing = await list_orders(client, token)
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 0


async def test_duplicate_order_created_event_delivered_once(client: Any) -> None:
    """Publishing the same event twice yields exactly one notification (dedupe)."""
    await clear_events(client)
    event = build_order_created_event(total_cents=4200)
    event_id = str(event["event_id"])
    headers = event_headers(event)

    await publish_order_created_event(event, headers)
    await publish_order_created_event(event, headers)

    async def _by_event_id() -> list[dict[str, Any]]:
        events = await get_events(client)
        return [item for item in events if item.get("event_id") == event_id]

    matches = await wait_for(_by_event_id, timeout=30.0, interval=0.5)
    assert len(matches) == 1, f"expected one delivery, got {len(matches)}"

    # Watch past the first retry tier (5 s) to prove no duplicate is redelivered.
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        current = await _by_event_id()
        assert len(current) == 1, f"duplicate delivery observed: {len(current)}"
        await asyncio.sleep(0.5)

    events = await get_events(client)
    assert len([item for item in events if item.get("event_id") == event_id]) == 1
    assert matches[0]["order_id"]
