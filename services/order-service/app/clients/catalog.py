"""HTTP client for catalog-service product lookups and stock reservations.

Catalog contract consumed here (owned by catalog-service):

* ``GET /products/{product_id}`` -> 200 ``ProductRead`` with ``id``, ``sku``,
  ``name``, ``description``, ``price_cents``, ``stock``, ``created_at`` and
  ``updated_at``; 404 ``product_not_found`` when unknown.
* ``POST /internal/stock/reserve`` with body
  ``{"product_id": "<uuid>", "quantity": <int > 0>}`` -> 200 with
  ``{product_id, reserved, stock}``; 409 ``insufficient_stock``; 404
  ``product_not_found``.
* ``POST /internal/stock/release`` with the same body shape -> 200 with
  ``{product_id, released, stock}``; 404 ``product_not_found``.

Both stock mutations require ``X-Internal-Token``. Product lookups (idempotent
GETs) are retried at most twice with jittered backoff; reserve/release mutations
are never retried automatically and are compensated explicitly by the caller.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.metrics import OrderMetrics
from kubecommerce_observability import ApiError

_MAX_GET_RETRIES = 2
_RETRY_BASE_DELAY_SECONDS = 0.02
_RETRY_MAX_DELAY_SECONDS = 0.2


class CatalogError(ApiError):
    """Base class for catalog-service failures that map to HTTP responses."""


class ProductNotFoundError(CatalogError):
    """The requested product does not exist in the catalog."""

    def __init__(self, product_id: UUID) -> None:
        super().__init__(
            "product_not_found",
            "Product not found",
            status_code=404,
            details={"product_id": str(product_id)},
        )


class CatalogTimeoutError(CatalogError):
    """The catalog did not respond before the request timeout."""

    def __init__(self) -> None:
        super().__init__("catalog_timeout", "Catalog request timed out", status_code=504)


class CatalogUnavailableError(CatalogError):
    """The catalog could not be reached or returned an unexpected response."""

    def __init__(self) -> None:
        super().__init__("catalog_unavailable", "Catalog service unavailable", status_code=502)


class InsufficientStockError(CatalogError):
    """One or more items could not be reserved."""

    def __init__(self, product_id: UUID) -> None:
        super().__init__(
            "insufficient_stock",
            "Insufficient stock for one or more items",
            status_code=409,
            details={"product_id": str(product_id)},
        )


class CatalogProduct(BaseModel):
    """The subset of a catalog ``ProductRead`` the order service relies on."""

    model_config = ConfigDict(extra="ignore")

    id: UUID
    sku: str
    price_cents: int = Field(ge=0)


@dataclass(frozen=True)
class PricedItem:
    """A requested item enriched with catalog sku and price."""

    product_id: UUID
    sku: str
    quantity: int
    unit_price_cents: int


def _stock_payload(item: PricedItem) -> dict[str, object]:
    """Build the request body shared by reserve and release."""
    return {"product_id": str(item.product_id), "quantity": item.quantity}


class CatalogClient:
    """Async catalog-service client bound to a configured ``httpx`` client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        internal_token: str,
        metrics: OrderMetrics | None = None,
        max_get_retries: int = _MAX_GET_RETRIES,
        base_delay_seconds: float = _RETRY_BASE_DELAY_SECONDS,
        max_delay_seconds: float = _RETRY_MAX_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._internal_token = internal_token
        self._metrics = metrics
        self._max_get_retries = max_get_retries
        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._sleep = sleep

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Internal-Token": self._internal_token}

    def _observe(self, operation: str, started: float) -> None:
        if self._metrics is not None:
            self._metrics.catalog_request_duration_seconds.labels(operation=operation).observe(
                time.perf_counter() - started
            )

    async def _backoff(self, attempt: int) -> None:
        delay = min(self._max_delay_seconds, self._base_delay_seconds * (2**attempt))
        jitter = random.uniform(0.0, self._base_delay_seconds)  # noqa: S311 - retry jitter
        await self._sleep(delay + jitter)

    async def fetch_product(self, product_id: UUID) -> CatalogProduct:
        """Fetch a product, retrying transport failures at most twice."""
        path = f"/products/{product_id}"
        attempts = self._max_get_retries + 1
        last_error: CatalogError = CatalogUnavailableError()
        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                response = await self._client.get(path, headers=self._headers)
            except httpx.TimeoutException:
                self._observe("get_product", started)
                last_error = CatalogTimeoutError()
            except httpx.RequestError:
                self._observe("get_product", started)
                last_error = CatalogUnavailableError()
            else:
                self._observe("get_product", started)
                if response.status_code == httpx.codes.NOT_FOUND:
                    raise ProductNotFoundError(product_id)
                if response.status_code >= httpx.codes.BAD_REQUEST:
                    raise CatalogUnavailableError()
                try:
                    return CatalogProduct.model_validate(response.json())
                except ValidationError as exc:
                    raise CatalogUnavailableError() from exc
            if attempt + 1 < attempts:
                await self._backoff(attempt)
        raise last_error

    async def reserve(self, item: PricedItem) -> None:
        """Reserve stock for a single item; raises on conflict or transport error."""
        started = time.perf_counter()
        try:
            response = await self._client.post(
                "/internal/stock/reserve",
                json=_stock_payload(item),
                headers=self._headers,
            )
        except httpx.TimeoutException as exc:
            self._observe("reserve", started)
            raise CatalogTimeoutError() from exc
        except httpx.RequestError as exc:
            self._observe("reserve", started)
            raise CatalogUnavailableError() from exc
        self._observe("reserve", started)
        if response.status_code == httpx.codes.NOT_FOUND:
            raise ProductNotFoundError(item.product_id)
        if response.status_code == httpx.codes.CONFLICT:
            raise InsufficientStockError(item.product_id)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise CatalogUnavailableError()

    async def release(self, item: PricedItem) -> None:
        """Release a previously reserved item (best-effort compensation)."""
        started = time.perf_counter()
        try:
            response = await self._client.post(
                "/internal/stock/release",
                json=_stock_payload(item),
                headers=self._headers,
            )
        except httpx.TimeoutException as exc:
            self._observe("release", started)
            raise CatalogTimeoutError() from exc
        except httpx.RequestError as exc:
            self._observe("release", started)
            raise CatalogUnavailableError() from exc
        self._observe("release", started)
        if response.status_code == httpx.codes.NOT_FOUND:
            raise ProductNotFoundError(item.product_id)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise CatalogUnavailableError()
