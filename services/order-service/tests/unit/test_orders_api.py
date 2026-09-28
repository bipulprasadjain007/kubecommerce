"""End-to-end tests for the order HTTP API (SQLite + respx catalog)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.models import Order
from kubecommerce_contracts import parse_event
from tests.support import (
    CATALOG_BASE_URL,
    auth_headers,
    get_orders,
    get_outbox,
    mock_product,
    mock_release,
    mock_reserve,
    seed_order,
)


def _body(*product_ids: object, quantity: int = 1) -> dict[str, object]:
    return {
        "items": [
            {"product_id": str(product_id), "quantity": quantity} for product_id in product_ids
        ]
    }


async def test_create_order_happy_path(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    user_id = uuid4()
    product_id = uuid4()
    mock_product(catalog_mock, product_id, sku="SKU-9", price=1999)
    reserve = mock_reserve(catalog_mock, return_value=httpx.Response(204))

    response = await client.post(
        "/orders",
        json=_body(product_id, quantity=2),
        headers=auth_headers(user_id),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert body["total_cents"] == 3998
    assert body["currency"] == "USD"
    assert body["items"] == [
        {
            "product_id": str(product_id),
            "sku": "SKU-9",
            "quantity": 2,
            "unit_price_cents": 1999,
        }
    ]
    assert reserve.called

    # The outbox row was written in the same transaction as the order.
    orders = await get_orders(app)
    outbox = await get_outbox(app)
    assert len(orders) == 1
    assert len(outbox) == 1
    event = parse_event(outbox[0].payload)
    assert str(event.data.order_id) == body["id"]
    assert event.data.total_cents == 3998
    assert event.data.user_id == user_id
    assert event.correlation_id


async def test_idempotent_replay_returns_same_order(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    user_id = uuid4()
    product_id = uuid4()
    mock_product(catalog_mock, product_id)
    reserve = mock_reserve(catalog_mock, return_value=httpx.Response(204))
    headers = auth_headers(user_id) | {"Idempotency-Key": "idem-1"}

    first = await client.post("/orders", json=_body(product_id), headers=headers)
    second = await client.post("/orders", json=_body(product_id), headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(await get_orders(app)) == 1
    assert len(await get_outbox(app)) == 1
    # The replay short-circuits before any catalog call.
    assert reserve.call_count == 1


async def test_insufficient_stock_compensates_prior_reservations(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    user_id = uuid4()
    first_product, second_product = uuid4(), uuid4()
    mock_product(catalog_mock, first_product)
    mock_product(catalog_mock, second_product)
    mock_reserve(
        catalog_mock,
        side_effect=[httpx.Response(204), httpx.Response(409)],
    )
    release = mock_release(catalog_mock, return_value=httpx.Response(204))

    response = await client.post(
        "/orders",
        json=_body(first_product, second_product),
        headers=auth_headers(user_id),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"
    assert release.call_count == 1
    assert await get_orders(app) == []
    assert await get_outbox(app) == []


async def test_catalog_timeout_during_reserve_compensates(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    user_id = uuid4()
    first_product, second_product = uuid4(), uuid4()
    mock_product(catalog_mock, first_product)
    mock_product(catalog_mock, second_product)
    mock_reserve(
        catalog_mock,
        side_effect=[httpx.Response(204), httpx.ConnectTimeout("slow")],
    )
    release = mock_release(catalog_mock, return_value=httpx.Response(204))

    response = await client.post(
        "/orders",
        json=_body(first_product, second_product),
        headers=auth_headers(user_id),
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "catalog_timeout"
    assert release.call_count == 1
    assert await get_orders(app) == []


async def test_product_lookup_timeout_is_retried_then_504(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    user_id = uuid4()
    product_id = uuid4()
    route = catalog_mock.get(f"{CATALOG_BASE_URL}/products/{product_id}").mock(
        side_effect=[httpx.ReadTimeout("t"), httpx.ReadTimeout("t"), httpx.ReadTimeout("t")]
    )

    response = await client.post(
        "/orders",
        json=_body(product_id),
        headers=auth_headers(user_id),
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "catalog_timeout"
    assert route.call_count == 3


async def test_product_lookup_connect_error_is_502(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    user_id = uuid4()
    product_id = uuid4()
    route = catalog_mock.get(f"{CATALOG_BASE_URL}/products/{product_id}").mock(
        side_effect=[httpx.ConnectError("no"), httpx.ConnectError("no"), httpx.ConnectError("no")]
    )

    response = await client.post(
        "/orders",
        json=_body(product_id),
        headers=auth_headers(user_id),
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "catalog_unavailable"
    assert route.call_count == 3


async def test_product_not_found_returns_404(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    user_id = uuid4()
    product_id = uuid4()
    catalog_mock.get(f"{CATALOG_BASE_URL}/products/{product_id}").mock(
        return_value=httpx.Response(404)
    )

    response = await client.post(
        "/orders",
        json=_body(product_id),
        headers=auth_headers(user_id),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "product_not_found"
    assert await get_orders(app) == []


async def test_product_lookup_malformed_body_is_502(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    user_id = uuid4()
    product_id = uuid4()
    catalog_mock.get(f"{CATALOG_BASE_URL}/products/{product_id}").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )

    response = await client.post(
        "/orders",
        json=_body(product_id),
        headers=auth_headers(user_id),
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "catalog_unavailable"


async def test_reserve_unexpected_status_is_502(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    user_id = uuid4()
    product_id = uuid4()
    mock_product(catalog_mock, product_id)
    mock_reserve(catalog_mock, return_value=httpx.Response(500))

    response = await client.post(
        "/orders",
        json=_body(product_id),
        headers=auth_headers(user_id),
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "catalog_unavailable"
    assert await get_orders(app) == []


async def test_release_failure_is_best_effort(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    user_id = uuid4()
    first_product, second_product = uuid4(), uuid4()
    mock_product(catalog_mock, first_product)
    mock_product(catalog_mock, second_product)
    mock_reserve(catalog_mock, side_effect=[httpx.Response(204), httpx.Response(409)])
    mock_release(catalog_mock, side_effect=httpx.ConnectError("no"))

    response = await client.post(
        "/orders",
        json=_body(first_product, second_product),
        headers=auth_headers(user_id),
    )

    # The original conflict still wins even though compensation failed.
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"


async def test_database_failure_releases_reservations(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    product_id = uuid4()
    mock_product(catalog_mock, product_id)
    mock_reserve(catalog_mock, return_value=httpx.Response(204))
    release = mock_release(catalog_mock, return_value=httpx.Response(204))

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr("app.service.repository.add_order", _boom)

    response = await client.post(
        "/orders",
        json=_body(product_id),
        headers=auth_headers(user_id),
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert release.call_count == 1
    assert await get_orders(app) == []


async def test_missing_user_context_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/orders",
        json=_body(uuid4()),
        headers={"X-Internal-Token": "test-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_user_context"


async def test_invalid_user_context_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/orders",
        json=_body(uuid4()),
        headers={"X-Internal-Token": "test-token", "X-User-ID": "not-a-uuid"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_user_context"


async def test_missing_internal_token_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/orders",
        json=_body(uuid4()),
        headers={"X-User-ID": str(uuid4())},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_internal_token"


async def test_invalid_internal_token_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/orders",
        json=_body(uuid4()),
        headers={"X-User-ID": str(uuid4()), "X-Internal-Token": "wrong-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_internal_token"


async def test_idempotency_race_recovers_existing_order(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    product_id = uuid4()
    idempotency_key = "race-key"
    mock_product(catalog_mock, product_id)
    reserve = mock_reserve(catalog_mock, return_value=httpx.Response(204))
    release = mock_release(catalog_mock, return_value=httpx.Response(204))
    seeded_id = await seed_order(
        app,
        user_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        idempotency_key=idempotency_key,
    )

    real_lookup = repository.get_order_by_idempotency_key
    calls = {"count": 0}

    async def _flaky_lookup(session: AsyncSession, lookup_key: str) -> Order | None:
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return await real_lookup(session, lookup_key)

    monkeypatch.setattr("app.service.repository.get_order_by_idempotency_key", _flaky_lookup)

    response = await client.post(
        "/orders",
        json=_body(product_id),
        headers=auth_headers(user_id) | {"Idempotency-Key": idempotency_key},
    )

    assert response.status_code == 201
    assert response.json()["id"] == str(seeded_id)
    assert calls["count"] >= 2
    orders = await get_orders(app)
    assert len(orders) == 1
    assert reserve.call_count == 1
    assert release.call_count == 1


async def test_get_order_enforces_ownership(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    owner, other = uuid4(), uuid4()
    product_id = uuid4()
    mock_product(catalog_mock, product_id)
    mock_reserve(catalog_mock, return_value=httpx.Response(204))

    created = await client.post(
        "/orders",
        json=_body(product_id),
        headers=auth_headers(owner),
    )
    order_id = created.json()["id"]

    owned = await client.get(f"/orders/{order_id}", headers=auth_headers(owner))
    assert owned.status_code == 200
    assert owned.json()["id"] == order_id

    hidden = await client.get(f"/orders/{order_id}", headers=auth_headers(other))
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "order_not_found"

    unknown = await client.get(f"/orders/{uuid4()}", headers=auth_headers(owner))
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "order_not_found"


async def test_list_orders_paginates_newest_first(app: FastAPI, client: httpx.AsyncClient) -> None:
    user_id = uuid4()
    seeded = [
        await seed_order(
            app,
            user_id,
            created_at=datetime(2026, 1, 1, 0, index, tzinfo=UTC),
        )
        for index in range(3)
    ]

    first_page = await client.get("/orders?limit=2&offset=0", headers=auth_headers(user_id))
    assert first_page.status_code == 200
    body = first_page.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2
    assert body["items"][0]["id"] == str(seeded[2])
    assert body["items"][1]["id"] == str(seeded[1])

    second_page = await client.get("/orders?limit=2&offset=2", headers=auth_headers(user_id))
    assert len(second_page.json()["items"]) == 1
    assert second_page.json()["items"][0]["id"] == str(seeded[0])

    other_user = await client.get("/orders", headers=auth_headers(uuid4()))
    assert other_user.json()["total"] == 0


async def test_invalid_idempotency_key_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/orders",
        json=_body(uuid4()),
        headers=auth_headers(uuid4()) | {"Idempotency-Key": "k" * 129},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"


async def test_invalid_request_body_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/orders",
        json={"items": [{"product_id": str(uuid4()), "quantity": 0}]},
        headers=auth_headers(uuid4()),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_reserve_request_matches_catalog_contract(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    """Reserve must send exactly {product_id, quantity} with the internal token."""
    user_id = uuid4()
    product_id = uuid4()
    mock_product(catalog_mock, product_id, sku="SKU-C1", price=750)
    reserve = mock_reserve(catalog_mock, return_value=httpx.Response(200, json={}))

    response = await client.post(
        "/orders",
        json=_body(product_id, quantity=3),
        headers=auth_headers(user_id),
    )

    assert response.status_code == 201
    assert response.json()["total_cents"] == 2250
    request = reserve.calls[0].request
    assert json.loads(request.content) == {"product_id": str(product_id), "quantity": 3}
    assert request.headers["X-Internal-Token"] == "test-token"


async def test_release_request_matches_catalog_contract(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog_mock: respx.Router,
) -> None:
    """Compensation must release exactly the reserved {product_id, quantity} pair."""
    user_id = uuid4()
    reserved, conflicting = uuid4(), uuid4()
    mock_product(catalog_mock, reserved, sku="SKU-C2", price=500)
    mock_product(catalog_mock, conflicting, sku="SKU-C3", price=500)
    mock_reserve(
        catalog_mock,
        side_effect=[
            httpx.Response(200, json={}),
            httpx.Response(409, json={"error": {"code": "insufficient_stock"}}),
        ],
    )
    release = mock_release(catalog_mock, return_value=httpx.Response(200, json={}))

    response = await client.post(
        "/orders",
        json=_body(reserved, conflicting, quantity=2),
        headers=auth_headers(user_id),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"
    request = release.calls[0].request
    assert json.loads(request.content) == {"product_id": str(reserved), "quantity": 2}
    assert request.headers["X-Internal-Token"] == "test-token"
