"""Cache-aside, invalidation and fail-open tests."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update

from app.models import Product

INTERNAL_HEADERS = {"X-Internal-Token": "test-internal-token"}


def _metric_sum(text: str, name: str) -> float:
    """Sum every sample of a metric whose name ends with ``name``."""
    total = 0.0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        series, _, value = line.rpartition(" ")
        metric_name = series.split("{", 1)[0]
        if metric_name.endswith(name):
            total += float(value)
    return total


async def _metric(client: AsyncClient, name: str) -> float:
    response = await client.get("/metrics")
    assert response.status_code == 200
    return _metric_sum(response.text, name)


async def _set_stock_directly(app: FastAPI, product_id: str, stock: int) -> None:
    async with app.state.session_factory() as session:
        await session.execute(
            update(Product).where(Product.id == uuid.UUID(product_id)).values(stock=stock)
        )
        await session.commit()


async def test_item_cache_miss_then_hit(
    client: AsyncClient, app: FastAPI, create_product: object
) -> None:
    product = await create_product(stock=10)  # type: ignore[operator]

    first = await client.get(f"/products/{product['id']}")
    assert first.status_code == 200
    assert first.json()["stock"] == 10

    # Bypass the app entirely: a cached read must not see this change.
    await _set_stock_directly(app, str(product["id"]), 999)

    second = await client.get(f"/products/{product['id']}")
    assert second.status_code == 200
    assert second.json()["stock"] == 10

    assert await _metric(client, "cache_misses_total") >= 1
    assert await _metric(client, "cache_hits_total") >= 1


async def test_list_cache_miss_then_hit(
    client: AsyncClient, app: FastAPI, create_product: object
) -> None:
    await create_product(sku="ONLY")  # type: ignore[operator]

    first = await client.get("/products")
    assert first.json()["total"] == 1

    async with app.state.session_factory() as session:
        session.add(Product(sku="EXTRA", name="Extra", price_cents=1, stock=1))
        await session.commit()

    second = await client.get("/products")
    assert second.json()["total"] == 1
    assert await _metric(client, "cache_hits_total") >= 1


async def test_cache_invalidated_after_patch(client: AsyncClient, create_product: object) -> None:
    product = await create_product(stock=10)  # type: ignore[operator]
    await client.get(f"/products/{product['id']}")

    patched = await client.patch(
        f"/products/{product['id']}/stock",
        json={"delta": 5},
        headers=INTERNAL_HEADERS,
    )
    assert patched.json()["stock"] == 15

    refreshed = await client.get(f"/products/{product['id']}")
    assert refreshed.json()["stock"] == 15


async def test_cache_invalidated_after_reserve_and_release(
    client: AsyncClient, create_product: object
) -> None:
    product = await create_product(stock=10)  # type: ignore[operator]
    await client.get(f"/products/{product['id']}")

    reserved = await client.post(
        "/internal/stock/reserve",
        json={"product_id": product["id"], "quantity": 4},
        headers=INTERNAL_HEADERS,
    )
    assert reserved.json()["stock"] == 6
    after_reserve = await client.get(f"/products/{product['id']}")
    assert after_reserve.json()["stock"] == 6

    released = await client.post(
        "/internal/stock/release",
        json={"product_id": product["id"], "quantity": 4},
        headers=INTERNAL_HEADERS,
    )
    assert released.json()["stock"] == 10
    after_release = await client.get(f"/products/{product['id']}")
    assert after_release.json()["stock"] == 10


async def test_cache_fail_open_serves_database(
    broken_client: AsyncClient, broken_app: FastAPI
) -> None:
    async with broken_app.state.session_factory() as session:
        session.add(Product(sku="FO", name="Fail open", price_cents=100, stock=3))
        await session.commit()

    response = await broken_client.get("/products")
    assert response.status_code == 200
    assert response.json()["total"] == 1

    item = response.json()["items"][0]
    single = await broken_client.get(f"/products/{item['id']}")
    assert single.status_code == 200

    assert await _metric(broken_client, "cache_errors_total") > 0


async def test_create_product_fail_open_cache(broken_client: AsyncClient) -> None:
    response = await broken_client.post(
        "/products",
        json={"sku": "FO-1", "name": "Fail open", "price_cents": 100, "stock": 3},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 201


async def test_readiness_is_database_only(broken_client: AsyncClient) -> None:
    response = await broken_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["database"] == "ok"


async def test_corrupt_item_falls_back_to_database(
    client: AsyncClient, fake_redis: object, create_product: object
) -> None:
    product = await create_product(stock=10)  # type: ignore[operator]
    await fake_redis.set(f"catalog:products:v1:item:{product['id']}", "not-json")  # type: ignore[attr-defined]

    response = await client.get(f"/products/{product['id']}")
    assert response.status_code == 200
    assert response.json()["stock"] == 10
    assert await _metric(client, "cache_errors_total") > 0


async def test_corrupt_list_falls_back_to_database(
    client: AsyncClient, fake_redis: object, create_product: object
) -> None:
    await create_product(stock=10)  # type: ignore[operator]
    await fake_redis.set("catalog:products:v1:list:20:0", "not-json")  # type: ignore[attr-defined]

    response = await client.get("/products")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert await _metric(client, "cache_errors_total") > 0


async def test_corrupt_version_falls_back_to_database(
    client: AsyncClient, fake_redis: object, create_product: object
) -> None:
    await create_product(stock=10)  # type: ignore[operator]
    await fake_redis.set("catalog:products:version", "not-a-number")  # type: ignore[attr-defined]

    response = await client.get("/products")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert await _metric(client, "cache_errors_total") > 0
