"""Redis cache-aside layer for catalog reads.

All Redis failures fail open: the error is logged, ``cache_errors_total`` is
incremented and the caller falls back to the database. Readiness never depends
on Redis.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from redis.asyncio import Redis

from app.metrics import CatalogMetrics
from app.schemas import ProductList, ProductRead
from kubecommerce_observability import get_logger

logger = get_logger("catalog.cache")

_VERSION_KEY = "catalog:products:version"

# Sentinel distinguishing "operation failed" from a legitimate cache miss.
_ERROR = object()


class CatalogCache:
    """Versioned, fail-open cache-aside helper for product reads."""

    def __init__(
        self,
        redis: Redis,
        ttl_seconds: int,
        metrics: CatalogMetrics,
    ) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._metrics = metrics

    async def _safe(self, operation: str, awaitable: Any) -> Any:
        """Await ``awaitable``, returning the error sentinel on any failure."""
        try:
            return await awaitable
        except Exception:
            self._metrics.cache_errors.labels(operation=operation).inc()
            logger.warning("cache_error", operation=operation)
            return _ERROR

    async def get_version(self) -> int:
        """Read the cache version once per request; 0 when unavailable."""
        raw = await self._safe("version", self._redis.get(_VERSION_KEY))
        if raw is _ERROR or raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            self._metrics.cache_errors.labels(operation="version_decode").inc()
            logger.warning("cache_error", operation="version_decode")
            return 0

    async def bump_version(self) -> None:
        """Invalidate every cached entry by advancing the version."""
        await self._safe("bump_version", self._redis.incr(_VERSION_KEY))

    @staticmethod
    def _item_key(version: int, product_id: uuid.UUID) -> str:
        return f"catalog:products:v{version}:item:{product_id}"

    @staticmethod
    def _list_key(version: int, limit: int, offset: int) -> str:
        return f"catalog:products:v{version}:list:{limit}:{offset}"

    async def get_item(self, version: int, product_id: uuid.UUID) -> ProductRead | None:
        """Return a cached product, or ``None`` on miss/corruption/error."""
        raw = await self._safe("get_item", self._redis.get(self._item_key(version, product_id)))
        if raw is _ERROR:
            return None
        if raw is None:
            self._metrics.cache_misses.inc()
            return None
        try:
            product = ProductRead.model_validate_json(raw)
        except ValidationError:
            self._metrics.cache_errors.labels(operation="deserialize_item").inc()
            logger.warning("cache_error", operation="deserialize_item")
            return None
        self._metrics.cache_hits.inc()
        return product

    async def set_item(self, version: int, product_id: uuid.UUID, product: ProductRead) -> None:
        """Cache a product under the given version (best effort)."""
        await self._safe(
            "set_item",
            self._redis.set(
                self._item_key(version, product_id),
                product.model_dump_json(),
                ex=self._ttl,
            ),
        )

    async def get_list(self, version: int, limit: int, offset: int) -> ProductList | None:
        """Return a cached product page, or ``None`` on miss/corruption/error."""
        raw = await self._safe("get_list", self._redis.get(self._list_key(version, limit, offset)))
        if raw is _ERROR:
            return None
        if raw is None:
            self._metrics.cache_misses.inc()
            return None
        try:
            page = ProductList.model_validate_json(raw)
        except ValidationError:
            self._metrics.cache_errors.labels(operation="deserialize_list").inc()
            logger.warning("cache_error", operation="deserialize_list")
            return None
        self._metrics.cache_hits.inc()
        return page

    async def set_list(self, version: int, limit: int, offset: int, page: ProductList) -> None:
        """Cache a product page under the given version (best effort)."""
        await self._safe(
            "set_list",
            self._redis.set(
                self._list_key(version, limit, offset),
                page.model_dump_json(),
                ex=self._ttl,
            ),
        )
