"""Route-level tests for authentication, header hygiene and proxying."""

from __future__ import annotations

from typing import Any

import httpx
from conftest import AUDIENCE, AUTH_BASE, ISSUER, JWKS_URL

from app.security import JWKSVerifier
from kubecommerce_observability import ApiError, create_http_client

INTERNAL_TOKEN = "test-internal-token"


async def test_valid_token_sets_verified_user_id(
    client: httpx.AsyncClient, http_mock: Any, make_token: Any
) -> None:
    route = http_mock.get("http://order-service/orders").mock(
        return_value=httpx.Response(200, json=[])
    )
    token = make_token(sub="user-42", email="u42@example.com")

    response = await client.get("/api/orders", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    downstream = route.calls.last.request
    assert downstream.headers["X-User-ID"] == "user-42"
    assert downstream.headers["X-Internal-Token"] == INTERNAL_TOKEN


async def test_order_proxy_forwards_idempotency_key_and_correlation_id(
    client: httpx.AsyncClient, http_mock: Any, make_token: Any
) -> None:
    route = http_mock.post("http://order-service/orders").mock(
        return_value=httpx.Response(201, json={"id": "o1"})
    )
    token = make_token(sub="user-7")

    response = await client.post(
        "/api/orders",
        json={"product_id": "p1"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "idem-123",
            "X-Correlation-ID": "corr-order-1",
        },
    )

    assert response.status_code == 201
    downstream = route.calls.last.request
    assert downstream.headers["X-User-ID"] == "user-7"
    assert downstream.headers["X-Internal-Token"] == INTERNAL_TOKEN
    assert downstream.headers["Idempotency-Key"] == "idem-123"
    assert downstream.headers["X-Correlation-ID"] == "corr-order-1"


async def test_order_client_supplied_trusted_headers_stripped_and_replaced(
    client: httpx.AsyncClient, http_mock: Any, make_token: Any
) -> None:
    route = http_mock.post("http://order-service/orders").mock(
        return_value=httpx.Response(201, json={"id": "o1"})
    )
    token = make_token(sub="user-7")

    response = await client.post(
        "/api/orders",
        json={"product_id": "p1"},
        headers={
            "Authorization": f"Bearer {token}",
            "X-User-ID": "attacker",
            "X-Internal-Token": "evil-token",
            "X-Forwarded-For": "9.9.9.9",
        },
    )

    assert response.status_code == 201
    downstream = route.calls.last.request
    assert downstream.headers["X-User-ID"] == "user-7"
    assert downstream.headers["X-Internal-Token"] == INTERNAL_TOKEN
    assert "X-Forwarded-For" not in downstream.headers


async def test_public_routes_need_no_token(client: httpx.AsyncClient, http_mock: Any) -> None:
    http_mock.get("http://catalog-service/products").mock(
        return_value=httpx.Response(200, json=[{"id": "p1"}])
    )

    response = await client.get("/api/catalog/products")

    assert response.status_code == 200
    assert response.json() == [{"id": "p1"}]


async def test_catalog_mutations_are_not_exposed(client: httpx.AsyncClient) -> None:
    create = await client.post("/api/catalog/products", json={"name": "widget"})
    update = await client.patch("/api/catalog/products/p1/stock", json={"stock": 3})

    # The list path exists for GET, so a POST is a method-not-allowed; the
    # stock path no longer exists at all.
    assert create.status_code == 405
    assert update.status_code == 404


async def test_missing_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/orders")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_token"


async def test_expired_token_is_401(client: httpx.AsyncClient, make_token: Any) -> None:
    token = make_token(exp_seconds=-30)

    response = await client.get("/api/orders", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "expired_token"


async def test_tampered_token_is_401(
    client: httpx.AsyncClient, make_token: Any, other_private_key: Any
) -> None:
    token = make_token(key=other_private_key)

    response = await client.get("/api/orders", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_readiness_ok_without_redis_or_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {}


async def test_unknown_kid_triggers_single_refresh(
    http_mock: Any,
    rsa_private_key: Any,
    other_private_key: Any,
    jwk_factory: Any,
    make_token: Any,
) -> None:
    route = http_mock.get(JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json={"keys": [jwk_factory(rsa_private_key, "key-a")]}),
            httpx.Response(
                200,
                json={
                    "keys": [
                        jwk_factory(rsa_private_key, "key-a"),
                        jwk_factory(other_private_key, "key-b"),
                    ]
                },
            ),
        ]
    )
    http_client = create_http_client(base_url=AUTH_BASE, timeout=5.0)
    verifier = JWKSVerifier(
        client=http_client,
        jwks_url=JWKS_URL,
        issuer=ISSUER,
        audience=AUDIENCE,
        cache_ttl=300.0,
        min_refresh_interval=10.0,
    )

    claims_a = await verifier.verify(make_token(key=rsa_private_key, kid_override="key-a"))
    assert claims_a["sub"] == "user-123"
    assert route.call_count == 1

    claims_b = await verifier.verify(make_token(key=other_private_key, kid_override="key-b"))
    assert claims_b["sub"] == "user-123"
    assert route.call_count == 2

    # A second unknown kid inside the minimum refresh interval must not re-fetch.
    with_unknown = make_token(key=other_private_key, kid_override="key-c")
    try:
        await verifier.verify(with_unknown)
        raise AssertionError("expected invalid_token")
    except ApiError as exc:
        assert exc.code == "invalid_token"
    assert route.call_count == 2

    await http_client.aclose()


async def test_all_route_mappings(
    client: httpx.AsyncClient, http_mock: Any, make_token: Any
) -> None:
    auth = make_token(sub="user-99", email="u99@example.com")
    bearer = {"Authorization": f"Bearer {auth}"}

    register = http_mock.post("http://auth-service/users").mock(
        return_value=httpx.Response(201, json={"id": "u1"})
    )
    login = http_mock.post("http://auth-service/login").mock(
        return_value=httpx.Response(200, json={"access_token": "t"})
    )
    me = http_mock.get("http://auth-service/me").mock(
        return_value=httpx.Response(200, json={"id": "u1"})
    )
    product = http_mock.get("http://catalog-service/products/p1").mock(
        return_value=httpx.Response(200, json={"id": "p1"})
    )
    order = http_mock.get("http://order-service/orders/o1").mock(
        return_value=httpx.Response(200, json={"id": "o1"})
    )

    assert (await client.post("/api/auth/users", json={})).status_code == 201
    assert (await client.post("/api/auth/login", json={})).status_code == 200
    assert (await client.get("/api/auth/me", headers=bearer)).status_code == 200
    assert (await client.get("/api/catalog/products/p1")).status_code == 200
    assert (await client.get("/api/orders/o1", headers=bearer)).status_code == 200

    assert register.called and login.called and me.called
    assert product.called and order.called
    assert order.calls.last.request.headers["X-User-ID"] == "user-99"
    assert order.calls.last.request.headers["X-Internal-Token"] == INTERNAL_TOKEN
