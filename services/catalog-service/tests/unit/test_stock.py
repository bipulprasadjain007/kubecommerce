"""Stock mutation (patch/reserve/release) tests."""

from __future__ import annotations

import uuid

from httpx import AsyncClient

INTERNAL_HEADERS = {"X-Internal-Token": "test-internal-token"}


async def test_patch_stock_increase(client: AsyncClient, create_product: object) -> None:
    product = await create_product(stock=10)  # type: ignore[operator]
    response = await client.patch(
        f"/products/{product['id']}/stock",
        json={"delta": 5},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["stock"] == 15


async def test_patch_stock_decrease(client: AsyncClient, create_product: object) -> None:
    product = await create_product(stock=10)  # type: ignore[operator]
    response = await client.patch(
        f"/products/{product['id']}/stock",
        json={"delta": -4},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["stock"] == 6


async def test_patch_stock_insufficient(client: AsyncClient, create_product: object) -> None:
    product = await create_product(stock=2)  # type: ignore[operator]
    response = await client.patch(
        f"/products/{product['id']}/stock",
        json={"delta": -5},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"


async def test_patch_stock_not_found(client: AsyncClient) -> None:
    response = await client.patch(
        f"/products/{uuid.uuid4()}/stock",
        json={"delta": 1},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "product_not_found"


async def test_patch_stock_delta_zero(client: AsyncClient, create_product: object) -> None:
    product = await create_product()  # type: ignore[operator]
    response = await client.patch(
        f"/products/{product['id']}/stock",
        json={"delta": 0},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 422


async def test_patch_stock_missing_token(client: AsyncClient, create_product: object) -> None:
    product = await create_product()  # type: ignore[operator]
    response = await client.patch(f"/products/{product['id']}/stock", json={"delta": 1})
    assert response.status_code == 401


async def test_reserve_stock_success(client: AsyncClient, create_product: object) -> None:
    product = await create_product(stock=10)  # type: ignore[operator]
    response = await client.post(
        "/internal/stock/reserve",
        json={"product_id": product["id"], "quantity": 4},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["product_id"] == product["id"]
    assert body["reserved"] == 4
    assert body["stock"] == 6


async def test_reserve_stock_conflict(client: AsyncClient, create_product: object) -> None:
    product = await create_product(stock=3)  # type: ignore[operator]
    response = await client.post(
        "/internal/stock/reserve",
        json={"product_id": product["id"], "quantity": 4},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"


async def test_reserve_stock_not_found(client: AsyncClient) -> None:
    response = await client.post(
        "/internal/stock/reserve",
        json={"product_id": str(uuid.uuid4()), "quantity": 1},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 404


async def test_reserve_stock_quantity_zero(client: AsyncClient, create_product: object) -> None:
    product = await create_product()  # type: ignore[operator]
    response = await client.post(
        "/internal/stock/reserve",
        json={"product_id": product["id"], "quantity": 0},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 422


async def test_reserve_stock_missing_token(client: AsyncClient, create_product: object) -> None:
    product = await create_product()  # type: ignore[operator]
    response = await client.post(
        "/internal/stock/reserve",
        json={"product_id": product["id"], "quantity": 1},
    )
    assert response.status_code == 401


async def test_release_stock_restores_stock(client: AsyncClient, create_product: object) -> None:
    product = await create_product(stock=10)  # type: ignore[operator]
    await client.post(
        "/internal/stock/reserve",
        json={"product_id": product["id"], "quantity": 4},
        headers=INTERNAL_HEADERS,
    )
    response = await client.post(
        "/internal/stock/release",
        json={"product_id": product["id"], "quantity": 4},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["released"] == 4
    assert body["stock"] == 10


async def test_release_stock_not_found(client: AsyncClient) -> None:
    response = await client.post(
        "/internal/stock/release",
        json={"product_id": str(uuid.uuid4()), "quantity": 1},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 404


async def test_release_stock_quantity_zero(client: AsyncClient, create_product: object) -> None:
    product = await create_product()  # type: ignore[operator]
    response = await client.post(
        "/internal/stock/release",
        json={"product_id": product["id"], "quantity": 0},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 422


async def test_release_stock_missing_token(client: AsyncClient, create_product: object) -> None:
    product = await create_product()  # type: ignore[operator]
    response = await client.post(
        "/internal/stock/release",
        json={"product_id": product["id"], "quantity": 1},
    )
    assert response.status_code == 401
