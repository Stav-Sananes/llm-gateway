"""The usage pipeline's adapters against real Redis and Postgres: stream, queue, store."""

import time
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from llm_gateway.config import Settings
from llm_gateway.main import build_engine, build_redis
from llm_gateway.storage.tables import usage_events
from llm_gateway.storage.usage_store import PostgresUsageStore
from llm_gateway.storage.usage_stream import (
    DEAD_LETTERS,
    STREAM,
    RedisUsageQueue,
    RedisUsageStream,
    decode,
    encode,
)
from llm_gateway.usage.model import Outcome
from llm_gateway.usage.ports import (
    InvalidUsageEventError,
    UsageQueueUnavailableError,
    UsageStoreUnavailableError,
)
from llm_gateway.usage.worker import Drained, UsageWorker
from tests.support.postgres import empty_tables
from tests.support.servers import unused_local_url
from tests.support.usage import a_usage_event

pytestmark = pytest.mark.anyio


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    # A socket timeout above the queue's block time, as the worker process uses.
    client = Redis.from_url(redis_url, socket_timeout=5)
    await client.flushdb()
    yield client
    await client.aclose()


@pytest.fixture
async def engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = build_engine(Settings(database_url=postgres_url))
    await empty_tables(engine)
    yield engine
    await engine.dispose()


def a_queue(redis: Redis, consumer: str = "w1", **options: Any) -> RedisUsageQueue:
    return RedisUsageQueue(redis, consumer, block_ms=100, **options)


async def stored_rows(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int(
            (await connection.execute(select(func.count()).select_from(usage_events))).scalar_one()
        )


def test_an_event_survives_the_encoding_round_trip() -> None:
    event = a_usage_event(provider=None, model=None, time_to_first_chunk_s=None)

    assert decode(encode(event)) == event


async def test_a_recorded_event_is_taken_by_the_worker(redis: Redis) -> None:
    queue = a_queue(redis)
    await queue.ensure_group()
    event = a_usage_event()

    await RedisUsageStream(redis).record(event)
    [delivery] = await queue.take(10)

    assert delivery.event == event


async def test_an_unacknowledged_event_is_delivered_again(redis: Redis) -> None:
    queue = a_queue(redis)
    await queue.ensure_group()
    await RedisUsageStream(redis).record(a_usage_event())

    [first] = await queue.take(10)
    [again] = await queue.take(10)  # the batch failed to store: no ack
    await queue.ack([again.delivery_id])

    assert again.delivery_id == first.delivery_id
    assert await queue.take(10) == []
    assert await redis.xlen(STREAM) == 0  # acknowledged entries are deleted


async def test_a_crashed_workers_events_are_claimed_by_another_once_stale(redis: Redis) -> None:
    crashed, survivor = a_queue(redis, "w1"), a_queue(redis, "w2", reclaim_idle_ms=50)
    await crashed.ensure_group()
    await RedisUsageStream(redis).record(a_usage_event())
    [taken] = await crashed.take(10)

    assert await survivor.take(10) == []  # not stale yet
    await anyio.sleep(0.1)
    [claimed] = await survivor.take(10)

    assert claimed.delivery_id == taken.delivery_id


async def test_an_undecodable_entry_is_delivered_without_an_event(redis: Redis) -> None:
    queue = a_queue(redis)
    await queue.ensure_group()
    await redis.xadd(STREAM, {"event": '{"event_id": "not a uuid"}'})

    [delivery] = await queue.take(10)
    await queue.dead_letter(delivery)

    assert delivery.event is None
    dead = await redis.xrange(DEAD_LETTERS)
    assert dead is not None and len(dead) == 1
    fields = dead[0][1]
    assert fields is not None and fields[b"event"] == b'{"event_id": "not a uuid"}'
    assert await queue.take(10) == []


async def test_the_queue_recovers_when_redis_lost_the_stream(redis: Redis) -> None:
    queue = a_queue(redis)
    await queue.ensure_group()
    await redis.flushdb()  # a restart without persistence
    event = a_usage_event()
    await RedisUsageStream(redis).record(event)  # XADD recreates the stream, not the group

    [delivery] = await queue.take(10)  # the group is recreated from the stream's start

    assert delivery.event == event


async def test_the_stream_is_capped(redis: Redis) -> None:
    stream = RedisUsageStream(redis, max_len=10)

    for _ in range(500):
        await stream.record(a_usage_event())

    assert await redis.xlen(STREAM) < 500


async def test_with_redis_down_an_event_is_dropped_and_counted_quickly() -> None:
    down = build_redis(Settings(redis_url=unused_local_url().replace("http://", "redis://")))
    dropped: list[int] = []
    started = time.monotonic()

    await RedisUsageStream(down, on_dropped=lambda: dropped.append(1)).record(a_usage_event())

    assert dropped == [1]
    assert time.monotonic() - started < 2
    with pytest.raises(UsageQueueUnavailableError):
        await a_queue(down).take(10)
    await down.aclose()


async def test_the_store_inserts_a_batch_and_skips_events_it_already_has(
    engine: AsyncEngine,
) -> None:
    store = PostgresUsageStore(engine)
    first, second = a_usage_event(), a_usage_event(outcome=Outcome.INTERRUPTED)

    await store.insert_many([first, second])
    await store.insert_many([first])

    assert await stored_rows(engine) == 2


async def test_the_store_refuses_bad_data_all_or_nothing(engine: AsyncEngine) -> None:
    with pytest.raises(InvalidUsageEventError):
        await PostgresUsageStore(engine).insert_many(
            [a_usage_event(), a_usage_event(charged_tokens=-1)]
        )

    assert await stored_rows(engine) == 0


async def test_the_store_reports_postgres_down() -> None:
    url = unused_local_url().replace("http://", "postgresql+asyncpg://u:p@")
    engine = build_engine(Settings(database_url=url, database_connect_timeout_s=0.5))
    try:
        with pytest.raises(UsageStoreUnavailableError):
            await PostgresUsageStore(engine).insert_many([a_usage_event()])
    finally:
        await engine.dispose()


async def test_the_worker_moves_events_from_redis_to_postgres(
    redis: Redis, engine: AsyncEngine
) -> None:
    queue = a_queue(redis)
    await queue.ensure_group()
    stream = RedisUsageStream(redis)
    for _ in range(3):
        await stream.record(a_usage_event())
    await redis.xadd(STREAM, {"event": "garbage"})

    result = await UsageWorker(queue, PostgresUsageStore(engine)).drain_once()

    assert result == Drained(written=3, dead_lettered=1)
    assert await stored_rows(engine) == 3
    assert await redis.xlen(STREAM) == 0
