"""Unit tests for :class:`NotificationProcessor` (no RabbitMQ)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fakeredis.aioredis import FakeRedis
from redis.exceptions import RedisError
from structlog.testing import capture_logs

from app.config import Settings
from app.consumer import (
    NotificationProcessor,
    WorkerMetrics,
    build_webhook,
)
from kubecommerce_contracts import (
    HEADER_RETRY_COUNT,
    HEADER_TRACEPARENT,
    OrderCreatedEvent,
    event_headers,
)
from kubecommerce_observability import ServiceMetrics
from tests.conftest import CORRELATION_ID, make_event


def _metrics() -> WorkerMetrics:
    return WorkerMetrics.create(ServiceMetrics("notification-worker"))


def _processor(
    redis: FakeRedis,
    settings: Settings,
    *,
    webhook: Any = None,
    metrics: WorkerMetrics | None = None,
) -> tuple[NotificationProcessor, AsyncMock, AsyncMock]:
    publish_retry = AsyncMock()
    publish_dlq = AsyncMock()
    processor = NotificationProcessor(
        redis=redis,  # type: ignore[arg-type]
        publish_retry=publish_retry,
        publish_dlq=publish_dlq,
        webhook=webhook,
        metrics=metrics or _metrics(),
        settings=settings,
    )
    return processor, publish_retry, publish_dlq


def _body(event: OrderCreatedEvent) -> bytes:
    return event.model_dump_json().encode("utf-8")


def _dedupe_key(event: OrderCreatedEvent) -> str:
    return f"dedupe:{event.event_id}"


class RedisErrorRedis(FakeRedis):
    """FakeRedis whose ``set`` always raises, simulating a Redis outage."""

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisError("redis unavailable")


async def test_happy_path_logs_and_marks_done(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    event = make_event()
    processor, publish_retry, publish_dlq = _processor(fake_redis, settings)

    with capture_logs() as logs:
        outcome = await processor.handle(_body(event), event_headers(event))

    assert outcome == "processed"
    assert await fake_redis.get(_dedupe_key(event)) == b"done"
    publish_retry.assert_not_awaited()
    publish_dlq.assert_not_awaited()

    sent = next(entry for entry in logs if entry.get("event") == "notification_sent")
    assert sent["event_id"] == str(event.event_id)
    assert sent["order_id"] == str(event.data.order_id)
    assert sent["user_id"] == str(event.data.user_id)
    assert sent["total_cents"] == event.data.total_cents
    assert sent["correlation_id"] == CORRELATION_ID


async def test_duplicate_done_skips_side_effect(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    event = make_event()
    await fake_redis.set(_dedupe_key(event), "done")
    webhook = AsyncMock()
    processor, publish_retry, publish_dlq = _processor(fake_redis, settings, webhook=webhook)

    outcome = await processor.handle(_body(event), event_headers(event))

    assert outcome == "duplicate"
    webhook.assert_not_awaited()
    publish_retry.assert_not_awaited()
    publish_dlq.assert_not_awaited()
    # The successful original claim is preserved.
    assert await fake_redis.get(_dedupe_key(event)) == b"done"


async def test_nx_conflict_processing_schedules_retry_without_side_effect(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    event = make_event()
    await fake_redis.set(_dedupe_key(event), "processing", ex=60)
    webhook = AsyncMock()
    processor, publish_retry, publish_dlq = _processor(fake_redis, settings, webhook=webhook)

    outcome = await processor.handle(_body(event), event_headers(event))

    assert outcome == "retry"
    webhook.assert_not_awaited()
    publish_retry.assert_awaited_once()
    assert publish_retry.await_args.args[2] == 1
    publish_dlq.assert_not_awaited()
    # The in-flight claim is preserved (never deleted, never dropped).
    assert await fake_redis.get(_dedupe_key(event)) == b"processing"


async def test_inflight_conflict_at_max_retries_goes_to_dlq(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    event = make_event()
    await fake_redis.set(_dedupe_key(event), "processing", ex=60)
    webhook = AsyncMock()
    processor, publish_retry, publish_dlq = _processor(fake_redis, settings, webhook=webhook)

    outcome = await processor.handle(
        _body(event), event_headers(event, retry_count=settings.worker_max_retries)
    )

    assert outcome == "dlq"
    webhook.assert_not_awaited()
    publish_retry.assert_not_awaited()
    publish_dlq.assert_awaited_once()
    assert await fake_redis.get(_dedupe_key(event)) == b"processing"


async def test_split_ttls_for_processing_and_done(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    event = make_event()
    recorded: list[tuple[str, Any, dict[str, Any]]] = []
    original_set = fake_redis.set

    async def spy_set(name: str, value: Any, **kwargs: Any) -> Any:
        recorded.append((name, value, kwargs))
        return await original_set(name, value, **kwargs)

    fake_redis.set = spy_set  # type: ignore[method-assign]
    processor, _, _ = _processor(fake_redis, settings)

    outcome = await processor.handle(_body(event), event_headers(event))

    assert outcome == "processed"
    processing = [call for call in recorded if call[1] == "processing"]
    done = [call for call in recorded if call[1] == "done"]
    assert settings.worker_processing_ttl_seconds != settings.worker_dedupe_ttl_seconds
    assert processing[0][2]["ex"] == settings.worker_processing_ttl_seconds
    assert done[0][2]["ex"] == settings.worker_dedupe_ttl_seconds


async def test_redis_error_on_claim_schedules_retry_without_side_effect(
    settings: Settings,
) -> None:
    event = make_event()
    webhook = AsyncMock()
    processor, publish_retry, publish_dlq = _processor(RedisErrorRedis(), settings, webhook=webhook)

    outcome = await processor.handle(_body(event), event_headers(event))

    assert outcome == "retry"
    webhook.assert_not_awaited()
    publish_retry.assert_awaited_once()
    assert publish_retry.await_args.args[2] == 1
    publish_dlq.assert_not_awaited()


async def test_redis_error_with_failing_retry_publish_propagates(
    settings: Settings,
) -> None:
    event = make_event()
    publish_retry = AsyncMock(side_effect=RuntimeError("broker down"))
    publish_dlq = AsyncMock()
    processor = NotificationProcessor(
        redis=RedisErrorRedis(),  # type: ignore[arg-type]
        publish_retry=publish_retry,
        publish_dlq=publish_dlq,
        webhook=None,
        metrics=_metrics(),
        settings=settings,
    )

    with pytest.raises(RuntimeError, match="broker down"):
        await processor.handle(_body(event), event_headers(event))

    publish_retry.assert_awaited_once()
    publish_dlq.assert_not_awaited()


async def test_webhook_failure_deletes_key_and_schedules_retry(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    event = make_event()
    webhook = AsyncMock(side_effect=httpx.ConnectError("webhook down"))
    metrics = _metrics()
    processor, publish_retry, publish_dlq = _processor(
        fake_redis, settings, webhook=webhook, metrics=metrics
    )

    outcome = await processor.handle(_body(event), event_headers(event))

    assert outcome == "retry"
    assert await fake_redis.get(_dedupe_key(event)) is None
    webhook.assert_awaited_once()
    publish_retry.assert_awaited_once()
    publish_dlq.assert_not_awaited()
    assert metrics.webhook_failures_total._value.get() == 1


async def test_retry_count_increments_over_tiers(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    event = make_event()
    webhook = AsyncMock(side_effect=RuntimeError("boom"))
    processor, publish_retry, _ = _processor(fake_redis, settings, webhook=webhook)

    for attempt in (1, 2, 3):
        headers = event_headers(event, retry_count=attempt - 1)
        outcome = await processor.handle(_body(event), headers)
        assert outcome == "retry"
        assert publish_retry.await_args.args[2] == attempt


async def test_after_max_retries_publishes_dlq(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    event = make_event()
    webhook = AsyncMock(side_effect=RuntimeError("boom"))
    processor, publish_retry, publish_dlq = _processor(fake_redis, settings, webhook=webhook)

    outcome = await processor.handle(
        _body(event), event_headers(event, retry_count=settings.worker_max_retries)
    )

    assert outcome == "dlq"
    publish_dlq.assert_awaited_once()
    publish_retry.assert_not_awaited()
    assert await fake_redis.get(_dedupe_key(event)) is None


async def test_poison_invalid_json_goes_to_dlq(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    processor, publish_retry, publish_dlq = _processor(fake_redis, settings)

    outcome = await processor.handle(b"{not valid json", {"x-retry-count": "2"})

    assert outcome == "poison"
    publish_dlq.assert_awaited_once()
    publish_retry.assert_not_awaited()


async def test_poison_unknown_event_version_goes_to_dlq(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    processor, publish_retry, publish_dlq = _processor(fake_redis, settings)
    body = json.dumps({"event_version": 99, "event_type": "order.created"}).encode()

    outcome = await processor.handle(body, {})

    assert outcome == "poison"
    publish_dlq.assert_awaited_once()
    publish_retry.assert_not_awaited()


async def test_retry_count_header_round_trip(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    event = make_event()
    webhook = AsyncMock(side_effect=RuntimeError("boom"))
    processor, publish_retry, _ = _processor(fake_redis, settings, webhook=webhook)
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    original: Mapping[str, Any] = {
        **event_headers(event, retry_count=0),
        HEADER_TRACEPARENT: traceparent,
    }

    await processor.handle(_body(event), original)

    _, published_headers, attempt = publish_retry.await_args.args
    assert attempt == 1
    expected = {**original, **event_headers(event, retry_count=1)}
    assert published_headers == expected
    assert published_headers[HEADER_RETRY_COUNT] == "1"
    # Original headers (e.g. traceparent) are copied through.
    assert published_headers[HEADER_TRACEPARENT] == traceparent


@pytest.mark.parametrize("raw", [None, "not-a-number", "-5"])
async def test_invalid_retry_count_defaults_to_zero(
    fake_redis: FakeRedis,
    settings: Settings,
    raw: str | None,
) -> None:
    event = make_event()
    webhook = AsyncMock(side_effect=RuntimeError("boom"))
    processor, publish_retry, _ = _processor(fake_redis, settings, webhook=webhook)
    headers: dict[str, Any] = event_headers(event)
    if raw is None:
        headers.pop(HEADER_RETRY_COUNT, None)
    else:
        headers[HEADER_RETRY_COUNT] = raw

    await processor.handle(_body(event), headers)

    assert publish_retry.await_args.args[2] == 1


@respx.mock
async def test_webhook_success_posts_expected_payload(
    fake_redis: FakeRedis,
    settings: Settings,
) -> None:
    event = make_event()
    route = respx.post("https://hooks.example/notify").mock(return_value=httpx.Response(204))
    webhook = build_webhook("https://hooks.example/notify", 5.0)
    processor, publish_retry, publish_dlq = _processor(fake_redis, settings, webhook=webhook)

    outcome = await processor.handle(_body(event), event_headers(event))

    assert outcome == "processed"
    assert route.called
    request = route.calls.last.request
    assert json.loads(request.content) == {
        "event_id": str(event.event_id),
        "order_id": str(event.data.order_id),
        "user_id": str(event.data.user_id),
        "total_cents": event.data.total_cents,
        "correlation_id": CORRELATION_ID,
    }
    assert request.headers["X-Correlation-ID"] == CORRELATION_ID
    publish_retry.assert_not_awaited()
    publish_dlq.assert_not_awaited()
