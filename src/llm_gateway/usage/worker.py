"""Drains usage events from the queue into the store, at least once.

    take a batch ─► dead-letter what can't be decoded ─► insert the rest (idempotent) ─► ack

Nothing is acknowledged until it is stored, so a crash or a store outage only delays
events: they are delivered again. If a batch fails while the store is up, the problem is
in the batch: the events are inserted one at a time and only the ones that fail are
dead-lettered, so one bad event can't block the queue. (Telling the two apart by the
error alone isn't safe: some refused values surface as generic driver errors.)
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import anyio

from llm_gateway.usage.model import UsageEvent
from llm_gateway.usage.ports import (
    Delivery,
    InvalidUsageEventError,
    UsageQueue,
    UsageQueueUnavailableError,
    UsageStore,
    UsageStoreUnavailableError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Drained:
    written: int = 0
    dead_lettered: int = 0
    lost: int = 0  # trimmed from the stream before they were stored


class WorkerObserver(Protocol):
    def drained(self, result: Drained) -> None: ...

    def failed(self, error: Exception) -> None: ...


class _Unobserved:
    def drained(self, result: Drained) -> None:
        pass

    def failed(self, error: Exception) -> None:
        pass


class UsageWorker:
    def __init__(
        self,
        queue: UsageQueue,
        store: UsageStore,
        batch_size: int = 500,
        observer: WorkerObserver | None = None,
    ) -> None:
        self._queue = queue
        self._store = store
        self._batch_size = batch_size
        self._observer = observer or _Unobserved()

    async def drain_once(self) -> Drained:
        """One batch. Raises UsageQueueUnavailableError or UsageStoreUnavailableError."""
        deliveries = await self._queue.take(self._batch_size)
        lost = [d for d in deliveries if d.lost]
        if lost:
            logger.error("%d usage events were trimmed before they were stored", len(lost))
            await self._queue.ack([d.delivery_id for d in lost])
        undecodable = [d for d in deliveries if d.event is None and not d.lost]
        decoded = [d for d in deliveries if d.event is not None]
        refused = await self._store_all(decoded)
        for delivery in undecodable + refused:
            logger.error("usage event %s dead-lettered", delivery.delivery_id)
            await self._queue.dead_letter(delivery)
        stored = [d.delivery_id for d in decoded if d not in refused]
        if stored:
            await self._queue.ack(stored)
        result = Drained(len(stored), len(undecodable) + len(refused), len(lost))
        self._observer.drained(result)
        return result

    async def run(
        self, retry_s: float = 1.0, max_retry_s: float = 30.0, idle_s: float = 0.1
    ) -> None:
        """Drain until cancelled, backing off while the queue or the store is down."""
        delay = retry_s
        while True:
            try:
                result = await self.drain_once()
            except (UsageQueueUnavailableError, UsageStoreUnavailableError) as error:
                self._observer.failed(error)
                logger.warning("usage worker waiting %.1fs: %s", delay, error)
                await anyio.sleep(delay)
                delay = min(delay * 2, max_retry_s)
                continue
            delay = retry_s
            if result == Drained():
                # The queue normally blocks while empty; this keeps a queue that doesn't
                # from turning the loop into a spin.
                await anyio.sleep(idle_s)

    async def _store_all(self, deliveries: Sequence[Delivery]) -> list[Delivery]:
        """Stores what it can; returns the deliveries that can't be stored. Raises
        UsageStoreUnavailableError only when the store itself is down."""
        try:
            await self._store.insert_many(_events(deliveries))
            return []
        except InvalidUsageEventError:
            pass
        except UsageStoreUnavailableError:
            if not await self._store.reachable():
                raise
        refused = []
        for delivery in deliveries:
            try:
                await self._store.insert_many(_events([delivery]))
            except InvalidUsageEventError:
                refused.append(delivery)
            except UsageStoreUnavailableError:
                if not await self._store.reachable():
                    raise
                refused.append(delivery)
        return refused


def _events(deliveries: Sequence[Delivery]) -> list[UsageEvent]:
    return [d.event for d in deliveries if d.event is not None]
