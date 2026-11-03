"""Usage events over a Redis Stream: the gateway appends, the usage worker drains.

The stream decouples the request path from Postgres: a slow or down database delays usage
rows, never a chat request. A consumer group gives at-least-once delivery: an entry stays
pending until the worker acknowledges it, and a crashed worker's pending entries are
claimed by another after `reclaim_idle_ms`.

The stream is capped (MAXLEN ~): when the worker is down long enough, the oldest unread
events are trimmed. Bounded memory beats a Redis that runs out of it.
"""

import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from llm_gateway.usage.model import Outcome, UsageEvent
from llm_gateway.usage.ports import Delivery, UsageQueueUnavailableError

logger = logging.getLogger(__name__)

STREAM = "gw:usage"
DEAD_LETTERS = "gw:usage:dead"
GROUP = "usage-writers"
_FIELD = "event"
_UNAVAILABLE = (RedisError, OSError, TimeoutError)


class RedisUsageStream:
    """The gateway's UsageRecorder. Never raises: a dropped event is logged and counted."""

    def __init__(
        self, redis: Redis, max_len: int = 100_000, on_dropped: Callable[[], None] = lambda: None
    ) -> None:
        self._redis = redis
        self._max_len = max_len
        self._on_dropped = on_dropped

    async def record(self, event: UsageEvent) -> None:
        try:
            await self._redis.xadd(
                STREAM, {_FIELD: encode(event)}, maxlen=self._max_len, approximate=True
            )
        except _UNAVAILABLE as error:
            logger.warning("usage event %s dropped: %s", event.event_id, type(error).__name__)
            self._on_dropped()


class RedisUsageQueue:
    """The usage worker's UsageQueue. Its Redis client's socket timeout must exceed
    `block_ms`, or every idle read times out."""

    def __init__(
        self,
        redis: Redis,
        consumer: str,
        block_ms: int = 5000,
        reclaim_idle_ms: int = 60_000,
        dead_letter_max_len: int = 10_000,
    ) -> None:
        self._redis = redis
        self._consumer = consumer
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._dead_letter_max_len = dead_letter_max_len

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise UsageQueueUnavailableError(str(error)) from error
        except _UNAVAILABLE as error:
            raise UsageQueueUnavailableError(type(error).__name__) from error

    async def take(self, max_events: int) -> Sequence[Delivery]:
        """Our own unacknowledged entries first (a batch that failed to store), then other
        consumers' stale ones, then new entries, waiting up to block_ms for them."""
        for attempt in (1, 2):
            try:
                entries = await self._entries(max_events)
                return [_delivery(entry_id, fields) for entry_id, fields in entries]
            except ResponseError as error:
                # Redis restarted without persistence: the stream and its group are gone.
                if "NOGROUP" not in str(error) or attempt == 2:
                    raise UsageQueueUnavailableError(str(error)) from error
                await self.ensure_group()
            except _UNAVAILABLE as error:
                raise UsageQueueUnavailableError(type(error).__name__) from error
        raise AssertionError("unreachable")

    async def ack(self, delivery_ids: Sequence[str]) -> None:
        if not delivery_ids:
            return
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.xack(STREAM, GROUP, *delivery_ids)
                pipe.xdel(STREAM, *delivery_ids)  # stored: no reason to keep it in memory
                await pipe.execute()
        except _UNAVAILABLE as error:
            raise UsageQueueUnavailableError(type(error).__name__) from error

    async def dead_letter(self, delivery: Delivery) -> None:
        try:
            await self._redis.xadd(
                DEAD_LETTERS,
                {"delivery_id": delivery.delivery_id, _FIELD: delivery.raw},
                maxlen=self._dead_letter_max_len,
                approximate=True,
            )
        except _UNAVAILABLE as error:
            raise UsageQueueUnavailableError(type(error).__name__) from error
        await self.ack([delivery.delivery_id])

    async def _entries(self, count: int) -> list[tuple[Any, Any]]:
        return await self._own_pending(count) or await self._stale(count) or await self._new(count)

    async def _own_pending(self, count: int) -> list[tuple[Any, Any]]:
        return _stream_entries(
            await self._redis.xreadgroup(GROUP, self._consumer, {STREAM: "0"}, count=count)
        )

    async def _stale(self, count: int) -> list[tuple[Any, Any]]:
        reply = await self._redis.xautoclaim(
            STREAM, GROUP, self._consumer, self._reclaim_idle_ms, start_id="0-0", count=count
        )
        claimed, trimmed = reply[1], reply[2] if len(reply) > 2 else []
        if trimmed:  # pending entries MAXLEN already removed: nothing left to deliver
            await self._redis.xack(STREAM, GROUP, *trimmed)
        return list(claimed)

    async def _new(self, count: int) -> list[tuple[Any, Any]]:
        return _stream_entries(
            await self._redis.xreadgroup(
                GROUP, self._consumer, {STREAM: ">"}, count=count, block=self._block_ms
            )
        )


def encode(event: UsageEvent) -> str:
    return json.dumps(
        {
            "event_id": str(event.event_id),
            "tenant_id": str(event.tenant_id),
            "requested_model": event.requested_model,
            "provider": event.provider,
            "model": event.model,
            "outcome": event.outcome.value,
            "prompt_tokens": event.prompt_tokens,
            "completion_tokens": event.completion_tokens,
            "charged_tokens": event.charged_tokens,
            "started_at": event.started_at.isoformat(),
            "duration_s": event.duration_s,
            "time_to_first_chunk_s": event.time_to_first_chunk_s,
        }
    )


def decode(raw: str) -> UsageEvent:
    """Raises ValueError, KeyError or TypeError on anything that isn't an encoded event."""
    data = json.loads(raw)
    started_at = datetime.fromisoformat(data["started_at"])
    if started_at.tzinfo is None:
        raise ValueError("started_at has no time zone")
    return UsageEvent(
        event_id=UUID(data["event_id"]),
        tenant_id=UUID(data["tenant_id"]),
        requested_model=str(data["requested_model"]),
        provider=_optional_str(data["provider"]),
        model=_optional_str(data["model"]),
        outcome=Outcome(data["outcome"]),
        prompt_tokens=_count(data["prompt_tokens"]),
        completion_tokens=_count(data["completion_tokens"]),
        charged_tokens=_count(data["charged_tokens"]),
        started_at=started_at,
        duration_s=float(data["duration_s"]),
        time_to_first_chunk_s=(
            None if data["time_to_first_chunk_s"] is None else float(data["time_to_first_chunk_s"])
        ),
    )


def _delivery(entry_id: Any, fields: Any) -> Delivery:
    delivery_id = _text(entry_id)
    raw = _text((fields or {}).get(_FIELD.encode(), (fields or {}).get(_FIELD, b"")))
    try:
        return Delivery(delivery_id, decode(raw), raw)
    except (ValueError, KeyError, TypeError, AttributeError):
        return Delivery(delivery_id, None, raw)


def _stream_entries(reply: Any) -> list[tuple[Any, Any]]:
    # XREADGROUP replies [[stream, [(id, fields), ...]]], or nothing.
    return [entry for _, entries in (reply or []) for entry in entries]


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"not a token count: {value!r}")
    return value
