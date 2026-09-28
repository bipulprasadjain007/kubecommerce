"""Transactional-outbox poller.

Events are written to the outbox table in the same transaction as the order, so
``POST /orders`` keeps succeeding while RabbitMQ is unavailable. This poller is
the only component that talks to the broker, and readiness deliberately checks
the database alone: the broker is a *degraded* dependency, never a fatal one.

Each pass selects pending rows (newest-tolerant ordering, batched) and publishes
them to the durable topic exchange :data:`EXCHANGE_EVENTS` with routing key
:data:`ROUTING_ORDER_CREATED`, persistent delivery and publisher confirms.
``published_at`` is set only after the broker confirms. Failures bump
``attempts``, store a sanitized ``last_error`` and schedule a capped exponential
backoff on ``next_attempt_at``.

On PostgreSQL the batch is selected ``FOR UPDATE SKIP LOCKED`` so multiple
replicas do not publish the same row concurrently; the guard is skipped on
SQLite so the test suite can run without PostgreSQL.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.metrics import OutboxMetrics
from app.models import OutboxEvent
from kubecommerce_contracts import (
    ROUTING_ORDER_CREATED,
    event_headers,
    parse_event,
)
from kubecommerce_observability import get_logger

logger = get_logger("kubecommerce.order.outbox")

#: Collaborator contract: body plus AMQP headers.
PublishCallable = Callable[[bytes, dict[str, str]], Awaitable[None]]

_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_CAP_SECONDS = 60.0
_MAX_ERROR_LENGTH = 200
_ERROR_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")


def sanitize_error(exc: BaseException) -> str:
    """Return a short, credential-free description of ``exc``."""
    message = str(exc).strip() or exc.__class__.__name__
    message = _ERROR_URL_RE.sub("<redacted-url>", message)
    return f"{exc.__class__.__name__}: {message[:_MAX_ERROR_LENGTH]}"


def backoff_delay_seconds(attempt: int, *, jitter: float = 0.0) -> float:
    """Return a capped exponential backoff for the 1-based failure ``attempt``."""
    exponent = max(0, attempt - 1)
    delay = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2**exponent))
    return min(_BACKOFF_CAP_SECONDS, delay + jitter)


class OutboxPoller:
    """Publishes pending outbox rows one batch at a time."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        publish: PublishCallable,
        interval_seconds: float,
        batch_size: int,
        dialect: str,
        metrics: OutboxMetrics,
    ) -> None:
        self._session_factory = session_factory
        self._publish = publish
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._dialect = dialect
        self._metrics = metrics

    async def run(self) -> None:
        """Poll forever until cancelled."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("outbox_poll_failed", error_type=exc.__class__.__name__)
            await asyncio.sleep(self._interval_seconds)

    async def poll_once(self) -> int:
        """Publish one batch, returning the number of rows published."""
        now = datetime.now(UTC)
        published = 0
        async with self._session_factory() as session:
            await self._update_gauges(session, now)
            statement = (
                select(OutboxEvent)
                .where(
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.next_attempt_at <= now,
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(self._batch_size)
            )
            if self._dialect == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            result = await session.execute(statement)
            for row in result.scalars().all():
                if await self._publish_row(row, now):
                    published += 1
            await session.commit()
        return published

    async def _update_gauges(self, session: AsyncSession, now: datetime) -> None:
        """Refresh the pending-count and oldest-pending-age gauges."""
        pending = await session.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None))
        )
        self._metrics.pending_events.set(int(pending or 0))

        oldest = await session.scalar(
            select(func.min(OutboxEvent.created_at)).where(OutboxEvent.published_at.is_(None))
        )
        if oldest is None:
            self._metrics.oldest_pending_seconds.set(0.0)
            return
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        self._metrics.oldest_pending_seconds.set(max(0.0, (now - oldest).total_seconds()))

    async def _publish_row(self, row: OutboxEvent, now: datetime) -> bool:
        """Publish one row, returning True on broker confirmation."""
        try:
            event = parse_event(row.payload)
            body = row.payload.encode("utf-8")
            await self._publish(body, event_headers(event, retry_count=0))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            row.attempts += 1
            row.last_error = sanitize_error(exc)
            jitter = random.uniform(0.0, 1.0)  # noqa: S311 - backoff jitter, not crypto
            row.next_attempt_at = now + timedelta(
                seconds=backoff_delay_seconds(row.attempts, jitter=jitter)
            )
            self._metrics.publish_failures_total.inc()
            logger.warning(
                "outbox_publish_failed",
                event_id=str(row.event_id),
                attempts=row.attempts,
                error_type=exc.__class__.__name__,
            )
            return False
        row.published_at = now
        row.last_error = None
        self._metrics.published_total.inc()
        logger.info(
            "outbox_published", event_id=str(row.event_id), routing_key=ROUTING_ORDER_CREATED
        )
        return True
