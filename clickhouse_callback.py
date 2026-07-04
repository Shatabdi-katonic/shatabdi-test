"""
LLM usage event callback.

Batches LLM usage events and emits them periodically or when the batch
reaches a threshold.  Operates in one of two sinks, chosen at runtime:

  * **Event bus (preferred).**  When an ``event_bus`` is wired (see
    ``set_event_bus``), each event is published to the ``llm.usage`` topic.
    The observability service consumes that topic and is the *sole* writer
    of the ``llm_usage_events`` ClickHouse table — as well as the only path
    that feeds Langfuse and the realtime Prometheus counters.  Publishing
    (rather than writing ClickHouse here too) keeps observability the single
    source of truth and avoids double-counting: ``llm_usage_events`` is a
    plain ``MergeTree`` with no dedup, so two writers would inflate every
    cost/usage figure.

  * **Direct ClickHouse (fallback).**  When no event bus is available
    (unit tests, or a deployment where the bus failed to initialise), the
    callback writes rows straight to ClickHouse exactly as before, so
    metering never silently disappears.  Because the bus is down in that
    case the consumer receives nothing, so there is still only one writer.

The observability service reads these rows for dashboards, cost
aggregation, and budget alerting.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from platform_core.database.clickhouse import ClickHouseManager

logger = logging.getLogger("gateway.clickhouse_callback")

# Topic the observability event consumer subscribes to for LLM metering.
_USAGE_TOPIC = "llm.usage"

_COLUMNS = [
    "event_id",
    "timestamp",
    "trace_id",
    "tenant_id",
    "user_id",
    "user_team",
    "agent_id",
    "agent_version",
    "session_id",
    "tier",
    "provider",
    "model",
    "tokens_input",
    "tokens_output",
    "cost_usd",
    "latency_ms",
    "ttft_ms",
    "status",
    "error_type",
    "is_fallback",
    "fallback_from",
    "is_cached",
]


@dataclass
class UsageEvent:
    """Single LLM usage event ready for ClickHouse insertion."""

    tenant_id: str = "default"
    user_id: str = ""
    user_team: str = ""
    agent_id: str = ""
    agent_version: str = ""
    session_id: str = ""
    trace_id: str = ""
    tier: str = ""
    provider: str = ""
    model: str = ""
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    ttft_ms: int | None = None
    status: str = "success"
    error_type: str | None = None
    is_fallback: bool = False
    fallback_from: str | None = None
    is_cached: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Serialise to the event-bus payload consumed by observability.

        Keys mirror the ``llm_usage_events`` column set the observability
        ``ClickHouseEventWriter`` expects (it maps payload keys → columns),
        and the realtime Langfuse / Prometheus sinks read the same dict.
        The timestamp is an ISO-8601 string because the event crosses Redis
        as JSON and cannot carry a native ``datetime``.
        """
        return {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trace_id": self.trace_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "user_team": self.user_team,
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "session_id": self.session_id,
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "ttft_ms": self.ttft_ms,
            "status": self.status,
            "error_type": self.error_type,
            "is_fallback": self.is_fallback,
            "fallback_from": self.fallback_from,
            "is_cached": self.is_cached,
        }

    def to_row(self) -> list[Any]:
        return [
            str(uuid.uuid4()),
            datetime.now(timezone.utc),
            self.trace_id,
            self.tenant_id,
            self.user_id,
            self.user_team,
            self.agent_id,
            self.agent_version,
            self.session_id,
            self.tier,
            self.provider,
            self.model,
            self.tokens_input,
            self.tokens_output,
            self.cost_usd,
            self.latency_ms,
            self.ttft_ms,
            self.status,
            self.error_type,
            1 if self.is_fallback else 0,
            self.fallback_from,
            1 if self.is_cached else 0,
        ]


class ClickHouseCallback:
    """Batched async emitter for LLM usage events.

    Call ``record()`` after each LLM call.  Events are accumulated in
    memory and flushed either when the batch is full or when the flush
    interval elapses.  The flush sink depends on configuration:

      * If an event bus has been attached via ``set_event_bus``, each event
        is **published** to the ``llm.usage`` topic and observability writes
        ClickHouse / Langfuse / Prometheus (sole-writer model).
      * Otherwise events are written **directly** to ClickHouse here.

    Call ``start()`` to launch the background flush task, and ``stop()``
    to flush remaining events and cancel the task.
    """

    def __init__(
        self,
        clickhouse: ClickHouseManager,
        table: str = "llm_usage_events",
        batch_size: int = 50,
        flush_interval: float = 5.0,
        max_buffer_size: int = 10000,
        event_bus: Any = None,
    ):
        self._ch = clickhouse
        self._table = table
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._max_buffer = max_buffer_size
        self._buffer: list[UsageEvent] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._event_bus = event_bus

    def set_event_bus(self, event_bus: Any) -> None:
        """Attach the platform event bus, switching the flush sink to publish.

        Called once the bus is initialised in the app lifespan.  After this,
        flushes publish ``llm.usage`` events instead of inserting into
        ClickHouse, making the observability consumer the sole writer.
        """
        self._event_bus = event_bus

    def start(self) -> None:
        """Launch the background flush loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Flush remaining events and cancel the background task."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._flush()

    async def record(self, event: UsageEvent) -> None:
        """Add a usage event to the buffer.  Flushes if batch is full."""
        async with self._lock:
            self._buffer.append(event)
            # Evict oldest events if buffer exceeds cap
            if len(self._buffer) > self._max_buffer:
                dropped = len(self._buffer) - self._max_buffer
                self._buffer = self._buffer[dropped:]
                logger.warning("ch_buffer_overflow", extra={"dropped": dropped})
            if len(self._buffer) >= self._batch_size:
                await self._flush_locked()

    async def _flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        """Flush the buffer to the active sink.  Caller must hold ``_lock``."""
        if not self._buffer:
            return
        batch = self._buffer[:]
        self._buffer.clear()
        try:
            if self._event_bus is not None:
                # Sole-writer model: publish; observability writes ClickHouse.
                for event in batch:
                    await self._event_bus.publish(
                        _USAGE_TOPIC, event.to_payload(), tenant_id=event.tenant_id
                    )
                logger.debug("usage_published", extra={"events": len(batch)})
            else:
                # Fallback: no bus configured, write ClickHouse directly.
                rows = [event.to_row() for event in batch]
                self._ch.insert_events(self._table, _COLUMNS, rows)
                logger.debug("ch_flush", extra={"rows": len(rows)})
        except Exception:
            logger.error("usage_flush_failed", extra={"events": len(batch)}, exc_info=True)
            # Re-enqueue failed events, respecting buffer cap
            combined = batch + self._buffer
            if len(combined) > self._max_buffer:
                dropped = len(combined) - self._max_buffer
                combined = combined[dropped:]
                logger.warning("usage_retry_overflow", extra={"dropped": dropped})
            self._buffer = combined

    async def _flush_loop(self) -> None:
        """Background loop that flushes the buffer periodically."""
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush()
