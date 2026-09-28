"""Tests for the transactional-outbox poller."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from app.outbox import backoff_delay_seconds, sanitize_error
from kubecommerce_contracts import event_headers, parse_event
from tests.support import FakePublisher, get_outbox, seed_outbox


async def test_poller_publishes_and_marks_published(app: FastAPI, publisher: FakePublisher) -> None:
    event = await seed_outbox(app)

    published = await app.state.outbox_poller.poll_once()

    assert published == 1
    assert len(publisher.published) == 1
    body, headers = publisher.published[0]

    # Payload + headers round-trip through the shared contracts helpers.
    assert parse_event(body) == event
    assert headers == event_headers(event, retry_count=0)
    assert headers["event_id"] == str(event.event_id)
    assert headers["event_type"] == "order.created"
    assert headers["event_version"] == "1"
    assert headers["x-retry-count"] == "0"

    rows = await get_outbox(app)
    assert rows[0].published_at is not None
    assert rows[0].attempts == 0
    assert rows[0].last_error is None

    # The gauge reflects pending rows at the start of the poll.
    assert app.state.outbox_metrics.pending_events._value.get() == 1

    # A follow-up poll with nothing to publish refreshes the gauges to zero.
    assert await app.state.outbox_poller.poll_once() == 0
    assert app.state.outbox_metrics.pending_events._value.get() == 0
    assert app.state.outbox_metrics.oldest_pending_seconds._value.get() == 0


async def test_poller_failure_increments_attempts_and_schedules_backoff(
    app: FastAPI, publisher: FakePublisher
) -> None:
    publisher.error = RuntimeError("amqp://user:secret@rabbitmq:5672/vhost refused")
    await seed_outbox(app)

    published = await app.state.outbox_poller.poll_once()

    assert published == 0
    rows = await get_outbox(app)
    row = rows[0]
    assert row.published_at is None
    assert row.attempts == 1
    assert row.last_error is not None
    assert "RuntimeError" in row.last_error
    assert "amqp://" not in row.last_error
    assert "<redacted-url>" in row.last_error

    assert row.next_attempt_at is not None
    scheduled = row.next_attempt_at
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=UTC)
    # Base backoff is 2s plus at most 1s of jitter.
    assert scheduled > datetime.now(UTC)
    assert scheduled <= datetime.now(UTC) + timedelta(seconds=3.5)


async def test_poller_skips_rows_not_yet_due(app: FastAPI, publisher: FakePublisher) -> None:
    await seed_outbox(app, next_attempt_at=datetime.now(UTC) + timedelta(hours=1))

    published = await app.state.outbox_poller.poll_once()

    assert published == 0
    assert publisher.published == []


async def test_poller_records_pending_gauges(app: FastAPI, publisher: FakePublisher) -> None:
    old = datetime(2020, 1, 1, tzinfo=UTC)
    await seed_outbox(app, created_at=old, next_attempt_at=old)
    await seed_outbox(app, created_at=old, next_attempt_at=old)
    publisher.error = RuntimeError("down")

    await app.state.outbox_poller.poll_once()

    metrics = app.state.outbox_metrics
    assert metrics.pending_events._value.get() == 2
    assert metrics.oldest_pending_seconds._value.get() > 0
    assert metrics.publish_failures_total._value.get() == 2


async def test_poller_publishes_only_batch_size(app: FastAPI, publisher: FakePublisher) -> None:
    app.state.outbox_poller._batch_size = 1
    await seed_outbox(app, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = await seed_outbox(app, created_at=datetime(2026, 1, 2, tzinfo=UTC))

    published = await app.state.outbox_poller.poll_once()

    assert published == 1
    assert len(publisher.published) == 1
    rows = await get_outbox(app)
    still_pending = [row for row in rows if row.published_at is None]
    assert len(still_pending) == 1
    assert still_pending[0].event_id == second.event_id


def test_backoff_delay_is_capped_and_exponential() -> None:
    assert backoff_delay_seconds(1, jitter=0.0) == 2.0
    assert backoff_delay_seconds(2, jitter=0.0) == 4.0
    assert backoff_delay_seconds(3, jitter=0.0) == 8.0
    assert backoff_delay_seconds(10, jitter=0.0) == 60.0
    assert backoff_delay_seconds(10, jitter=5.0) == 60.0


def test_sanitize_error_redacts_urls() -> None:
    message = sanitize_error(RuntimeError("connect amqp://user:pass@host/vhost failed"))

    assert message.startswith("RuntimeError:")
    assert "<redacted-url>" in message
    assert "pass" not in message
    assert sanitize_error(ValueError("")) == "ValueError: ValueError"
