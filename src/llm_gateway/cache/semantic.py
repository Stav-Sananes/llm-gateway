"""The semantic cache: answer a question asked before (in meaning) without calling a model.

    eligible? ─no─► bypass
       │ yes
    embed the question          (its own circuit breaker; a hung embedder counts as failing)
    nearest answers, same tenant + same context, score ≥ threshold   (Qdrant's own breaker)
    first one whose question also agrees word for word (numbers, negations, overlap)
       ├─ found ─► hit: replay it
       └─ none  ─► miss: call the provider; a clean answer is queued to be stored

The cache never fails a request: every error is a bypass. Writes happen in the background,
off the response path, through a bounded queue that drops when full.
"""

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from llm_gateway.cache.model import (
    MAX_CACHED_ANSWER_CHARS,
    CachedAnswer,
    CacheDirectives,
    CacheKey,
    cache_key,
    lexically_compatible,
)
from llm_gateway.cache.ports import AnswerStore, Embedder, Neighbour
from llm_gateway.chat.model import ChatRequest, CompletionChunk, Origin, Usage
from llm_gateway.chat.ports import ChunkStream
from llm_gateway.routing.breaker import BreakerState, CircuitBreaker

logger = logging.getLogger(__name__)

CACHE_PROVIDER = "cache"  # the Origin provider of a replayed answer
_CANDIDATES = 3  # nearest answers checked against the word-for-word guard
WARM_UP_TEXT = "warm up"


class LookupResult(StrEnum):
    HIT = "hit"
    MISS = "miss"
    INELIGIBLE = "ineligible"  # tools, temperature, images, or the client opted out
    UNAVAILABLE = "unavailable"  # an error, or a circuit is open


@dataclass(frozen=True)
class Hit:
    answer: CachedAnswer
    score: float


@dataclass(frozen=True)
class Miss:
    """A lookup that found nothing; carries what's needed to remember the answer."""

    key: CacheKey
    vector: tuple[float, ...]
    store: bool


Lookup = Hit | Miss | None  # None: bypassed


class CacheObserver(Protocol):
    def looked_up(self, result: LookupResult, score: float | None) -> None: ...

    def write_dropped(self) -> None: ...


class _Unobserved:
    def looked_up(self, result: LookupResult, score: float | None) -> None:
        pass

    def write_dropped(self) -> None:
        pass


@dataclass(frozen=True)
class CacheSettings:
    similarity_threshold: float = 0.95
    ttl_s: float = 3600.0
    timeout_s: float = 1.0  # embed + search together; past it, the request goes on uncached
    write_queue: int = 100  # answers waiting to be stored; more are dropped


DEFAULT_CACHE_SETTINGS = CacheSettings()


@dataclass(frozen=True)
class CacheBreakers:
    """One per dependency: a healthy embedder must not hide a hung Qdrant."""

    embedder: CircuitBreaker
    store: CircuitBreaker


@dataclass(frozen=True)
class _Write:
    miss: Miss
    answer: CachedAnswer


class SemanticCache:
    def __init__(
        self,
        embedder: Embedder,
        store: AnswerStore,
        breakers: CacheBreakers,
        settings: CacheSettings = DEFAULT_CACHE_SETTINGS,
        observer: CacheObserver | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._breakers = breakers
        self._settings = settings
        self._observer = observer or _Unobserved()
        self._clock = clock
        self._writes_in: MemoryObjectSendStream[_Write]
        self._writes_out: MemoryObjectReceiveStream[_Write]
        self._writes_in, self._writes_out = anyio.create_memory_object_stream[_Write](
            settings.write_queue
        )

    async def look_up(
        self, tenant_id: UUID, request: ChatRequest, directives: CacheDirectives
    ) -> Lookup:
        key = cache_key(tenant_id, request)
        if key is None or not (directives.read or directives.write):
            self._observer.looked_up(LookupResult.INELIGIBLE, None)
            return None
        if self._breakers.store.state is BreakerState.OPEN:
            # Nothing could be read or stored: don't pay for an embedding either.
            self._observer.looked_up(LookupResult.UNAVAILABLE, None)
            return None
        deadline = anyio.current_time() + self._settings.timeout_s
        vector = await self._call(
            self._breakers.embedder, deadline, lambda: self._embedder.embed(key.question)
        )
        if vector is None:
            self._observer.looked_up(LookupResult.UNAVAILABLE, None)
            return None
        candidates: Sequence[Neighbour] | None = []
        if directives.read:
            candidates = await self._call(
                self._breakers.store, deadline, lambda: self._nearest(key, vector)
            )
        if candidates is None:
            self._observer.looked_up(LookupResult.UNAVAILABLE, None)
            return None
        for neighbour in candidates:
            if lexically_compatible(key.question, neighbour.question):
                self._observer.looked_up(LookupResult.HIT, neighbour.score)
                return Hit(neighbour.answer, neighbour.score)
        self._observer.looked_up(LookupResult.MISS, None)
        return Miss(key, tuple(vector), store=directives.write)

    async def warm_up(self, within_s: float = 60.0, every_s: float = 1.0) -> bool:
        """Embed once before traffic needs it; True once the embedder has answered.

        A cold embedding model can take longer to load than a lookup may wait: until it
        has loaded, every lookup times out, and the breaker opens. So try, off the request
        path and outside the breaker, until one call succeeds or within_s has passed."""
        with anyio.move_on_after(within_s):
            while True:
                try:
                    await self._embedder.embed(WARM_UP_TEXT)
                    return True
                except Exception as error:
                    logger.info("semantic cache embedder not ready: %s", type(error).__name__)
                await anyio.sleep(every_s)
        logger.warning("semantic cache embedder still not answering after %ss", within_s)
        return False

    def remember(self, miss: Miss, answer: CachedAnswer) -> None:
        """Queue a fresh answer to be stored. Never blocks, never raises; only clean,
        bounded answers are kept, and a full queue drops the write."""
        if not miss.store or answer.finish_reason != "stop" or not answer.content:
            return
        if len(answer.content) > MAX_CACHED_ANSWER_CHARS:
            return
        try:
            self._writes_in.send_nowait(_Write(miss, answer))
        except anyio.WouldBlock:
            self._observer.write_dropped()

    async def run_writer(self) -> None:
        """Store queued answers until cancelled. The app's lifespan runs it."""
        async for write in self._writes_out:
            await self._store_one(write)

    async def write_pending(self) -> None:
        """Store whatever is queued now (tests, and a drain on shutdown)."""
        while True:
            try:
                write = self._writes_out.receive_nowait()
            except anyio.WouldBlock:
                return
            await self._store_one(write)

    async def _store_one(self, write: _Write) -> None:
        expires_at = self._clock() + self._settings.ttl_s
        deadline = anyio.current_time() + self._settings.timeout_s
        await self._call(
            self._breakers.store,
            deadline,
            lambda: self._store.put(write.miss.key, write.miss.vector, write.answer, expires_at),
        )

    async def _nearest(self, key: CacheKey, vector: Sequence[float]) -> Sequence[Neighbour]:
        return await self._store.nearest(
            key, vector, self._settings.similarity_threshold, self._clock(), _CANDIDATES
        )

    async def _call[T](
        self, breaker: CircuitBreaker, deadline: float, operation: Callable[[], Awaitable[T]]
    ) -> T | None:
        """Run operation behind its breaker until the deadline; None when it couldn't.

        Any Exception counts as the dependency failing (a malformed reply is a failure too);
        cancellation isn't an Exception, and releases the breaker's permit untouched."""
        permit = breaker.try_acquire()
        if permit is None:
            return None
        outcome_recorded = False
        try:
            with anyio.fail_after(max(0.0, deadline - anyio.current_time())):
                result = await operation()
            breaker.record_success()
            outcome_recorded = True
            return result
        except Exception as error:
            logger.warning("semantic cache bypassed (%s): %s", breaker.name, type(error).__name__)
            breaker.record_failure()
            outcome_recorded = True
            return None
        finally:
            if not outcome_recorded:
                breaker.release(permit)


async def replay(hit: Hit) -> ChunkStream:
    """A cached answer as a provider stream: free (zero usage), marked as from the cache."""
    yield CompletionChunk(
        content=hit.answer.content, origin=Origin(CACHE_PROVIDER, hit.answer.model)
    )
    yield CompletionChunk(finish_reason=hit.answer.finish_reason)
    yield CompletionChunk(usage=Usage(0, 0))
