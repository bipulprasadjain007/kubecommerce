"""End-to-end integration tests for the KubeCommerce happy paths.

These tests require a running docker compose stack and are skipped cleanly
(otherwise) by the ``require_stack`` fixture in ``conftest.py``.

``curl`` equivalents::

    curl -s localhost:8080/health/ready
    curl -s -X POST localhost:8080/api/auth/users -H 'content-type: application/json' \\
         -d '{"email":"a@b.c","password":"integration-password-123"}'
    curl -s -X POST localhost:8002/products -H "X-Internal-Token: $TOKEN" \\
         -H 'content-type: application/json' -d '{"sku":"S","name":"N","price_cents":100,"stock":5}'
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest

from conftest import (
    CATALOG_URL,
    GATEWAY_URL,
    auth_headers,
    clear_events,
    create_order,
    create_product,
    get_events,
    get_order,
    list_orders,
    login,
    register_user,
    unique_email,
    unique_sku,
    wait_for,
)

pytestmark = pytest.mark.integration


async def _matching_events(client: Any, order_id: str) -> list[dict[str, Any]]:
    """Return sink events whose ``order_id`` matches ``order_id``."""
    events = await get_events(client)
    return [event for event in events if event.get("order_id") == order_id]


async def _assert_single_delivery(client: Any, order_id: str, grace: float = 8.0) -> None:
    """Poll for ``grace`` seconds and assert exactly one delivery ever lands."""
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        matches = await _matching_events(client, order_id)
        assert len(matches) == 1, f"expected exactly one notification, got {len(matches)}"
        await asyncio.sleep(0.5)


async def test_gateway_ready_register_login_me(client: Any) -> None:
    """Gateway is ready; a registered user can log in and read ``/me``."""
    ready = await client.get(f"{GATEWAY_URL}/health/ready")
    assert ready.status_code == 200

    email = unique_email()
    user = await register_user(client, email=email)
    assert user["email"].lower() == email.lower()
    assert uuid.UUID(user["id"])

    token = await login(client, email)
    me = await client.get(f"{GATEWAY_URL}/api/auth/me", headers=auth_headers(token))
    assert me.status_code == 200
    assert me.json()["email"].lower() == email.lower()


async def test_order_flow_creates_order_and_notifies_once(client: Any) -> None:
    """register -> login -> product -> order -> event -> notification worker."""
    email = unique_email()
    await register_user(client, email=email)
    token = await login(client, email)

    product = await create_product(client, sku=unique_sku(), price_cents=2500, stock=10)
    await clear_events(client)

    response = await create_order(client, token, [{"product_id": product["id"], "quantity": 2}])
    assert response.status_code == 201, response.text
    order = response.json()
    assert order["total_cents"] == 5000
    assert order["currency"] == "USD"
    assert order["items"][0]["unit_price_cents"] == 2500
    assert order["items"][0]["sku"] == product["sku"]

    fetched = await get_order(client, token, order["id"])
    assert fetched.status_code == 200
    assert fetched.json()["id"] == order["id"]

    matches = await wait_for(
        lambda: _matching_events(client, order["id"]), timeout=30.0, interval=0.5
    )
    assert len(matches) == 1
    assert matches[0]["event_id"]
    assert matches[0]["total_cents"] == 5000

    # A duplicate delivery (at-least-once + dedupe) must never reach the sink.
    await _assert_single_delivery(client, order["id"])


async def test_idempotency_key_replays_same_order(client: Any) -> None:
    """Repeating an ``Idempotency-Key`` returns the original order, not a new one."""
    email = unique_email()
    await register_user(client, email=email)
    token = await login(client, email)
    product = await create_product(client, price_cents=1234, stock=10)
    items = [{"product_id": product["id"], "quantity": 1}]
    key = f"it-{uuid.uuid4().hex}"

    first = await create_order(client, token, items, idempotency_key=key)
    second = await create_order(client, token, items, idempotency_key=key)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]

    listing = await list_orders(client, token)
    assert listing.status_code == 200
    ids = [order["id"] for order in listing.json()["items"]]
    assert ids.count(first.json()["id"]) == 1


async def test_order_authn_and_ownership(client: Any) -> None:
    """Missing/bogus tokens are rejected and orders are only visible to owners."""
    email = unique_email()
    await register_user(client, email=email)
    token = await login(client, email)
    product = await create_product(client, price_cents=999, stock=10)
    items = [{"product_id": product["id"], "quantity": 1}]

    anonymous = await client.post(f"{GATEWAY_URL}/api/orders", json={"items": items})
    assert anonymous.status_code == 401

    bogus = await client.post(
        f"{GATEWAY_URL}/api/orders",
        json={"items": items},
        headers=auth_headers("not-a-real-token"),
    )
    assert bogus.status_code == 401

    created = await create_order(client, token, items)
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]

    other_email = unique_email("other")
    await register_user(client, email=other_email)
    other_token = await login(client, other_email)
    other_read = await get_order(client, other_token, order_id)
    assert other_read.status_code == 404


async def test_catalog_mutations_are_not_public(client: Any) -> None:
    """The gateway must not expose catalog-service mutation endpoints."""
    response = await client.post(
        f"{GATEWAY_URL}/api/catalog/products",
        json={"sku": unique_sku(), "name": "nope", "price_cents": 1, "stock": 1},
        headers={"X-Internal-Token": "irrelevant"},
    )
    assert response.status_code in {404, 405}

    # ...and the direct catalog endpoint still requires the internal token.
    direct = await client.post(
        f"{CATALOG_URL}/products",
        json={"sku": unique_sku(), "name": "nope", "price_cents": 1, "stock": 1},
    )
    assert direct.status_code in {401, 403}
