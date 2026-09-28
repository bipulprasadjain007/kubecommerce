"""Product create/list/get endpoint tests."""

from __future__ import annotations

import uuid

from httpx import AsyncClient

INTERNAL_HEADERS = {"X-Internal-Token": "test-internal-token"}


async def test_create_product_happy_path(client: AsyncClient) -> None:
    response = await client.post(
        "/products",
        json={
            "sku": "SKU-1",
            "name": "Widget",
            "description": "A widget",
            "price_cents": 1000,
            "stock": 10,
        },
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["sku"] == "SKU-1"
    assert body["name"] == "Widget"
    assert body["description"] == "A widget"
    assert body["price_cents"] == 1000
    assert body["stock"] == 10
    uuid.UUID(body["id"])
    assert body["created_at"]
    assert body["updated_at"]


async def test_create_product_optional_description(client: AsyncClient) -> None:
    response = await client.post(
        "/products",
        json={"sku": "SKU-2", "name": "No description", "price_cents": 1, "stock": 0},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 201
    assert response.json()["description"] is None


async def test_create_product_duplicate_sku(client: AsyncClient, create_product: object) -> None:
    await create_product(sku="DUP")  # type: ignore[operator]
    response = await client.post(
        "/products",
        json={"sku": "DUP", "name": "Again", "price_cents": 1, "stock": 1},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "sku_already_exists"


async def test_create_product_duplicate_sku_race(
    client: AsyncClient, create_product: object, monkeypatch: object
) -> None:
    """A concurrent insert (pre-check misses it) is caught as IntegrityError."""
    await create_product(sku="RACE")  # type: ignore[operator]

    async def _no_existing(session: object, sku: str) -> None:
        return None

    monkeypatch.setattr("app.service.repo.get_by_sku", _no_existing)  # type: ignore[attr-defined]
    response = await client.post(
        "/products",
        json={"sku": "RACE", "name": "Again", "price_cents": 1, "stock": 1},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "sku_already_exists"


async def test_create_product_negative_price(client: AsyncClient) -> None:
    response = await client.post(
        "/products",
        json={"sku": "NEG", "name": "Bad", "price_cents": -1, "stock": 1},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_create_product_negative_stock(client: AsyncClient) -> None:
    response = await client.post(
        "/products",
        json={"sku": "NEG", "name": "Bad", "price_cents": 1, "stock": -1},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 422


async def test_create_product_missing_token(client: AsyncClient) -> None:
    response = await client.post(
        "/products",
        json={"sku": "SKU", "name": "N", "price_cents": 1, "stock": 1},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_internal_token"


async def test_create_product_invalid_token(client: AsyncClient) -> None:
    response = await client.post(
        "/products",
        json={"sku": "SKU", "name": "N", "price_cents": 1, "stock": 1},
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_internal_token"


async def test_list_products(client: AsyncClient, create_product: object) -> None:
    await create_product(sku="A")  # type: ignore[operator]
    await create_product(sku="B")  # type: ignore[operator]
    response = await client.get("/products")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert body["limit"] == 20
    assert body["offset"] == 0


async def test_list_products_pagination(client: AsyncClient, create_product: object) -> None:
    for index in range(3):
        await create_product(sku=f"P-{index}")  # type: ignore[operator]
    page = await client.get("/products", params={"limit": 2, "offset": 0})
    assert len(page.json()["items"]) == 2
    assert page.json()["total"] == 3
    last = await client.get("/products", params={"limit": 2, "offset": 2})
    assert len(last.json()["items"]) == 1


async def test_list_products_limit_too_high(client: AsyncClient) -> None:
    response = await client.get("/products", params={"limit": 101})
    assert response.status_code == 422


async def test_list_products_limit_zero(client: AsyncClient) -> None:
    response = await client.get("/products", params={"limit": 0})
    assert response.status_code == 422


async def test_get_product(client: AsyncClient, create_product: object) -> None:
    created = await create_product()  # type: ignore[operator]
    response = await client.get(f"/products/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_get_product_not_found(client: AsyncClient) -> None:
    response = await client.get(f"/products/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "product_not_found"


async def test_get_product_invalid_uuid(client: AsyncClient) -> None:
    response = await client.get("/products/not-a-uuid")
    assert response.status_code == 422
