"""The semantic cache: answer a question asked before (in meaning) without calling a model.

    eligible? ─no─► bypass
       │ yes
    breaker open? ─yes─► bypass                      (Qdrant or the embedder is down)
       │ no
    embed the question ─► nearest answer, same tenant + same context, score ≥ threshold
       ├─ found ─► hit: replay it
       └─ none  ─► miss: call the provider, and remember the answer if it finished cleanly

The cache never fails a request: every error is a bypass, and repeated errors open its
circuit so a dead Qdrant costs nothing per request.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import anyio

from llm_gateway.cache.model import (
    MAX_CACHED_ANSWER_CHARS,
    CachedAnswer,
    CacheDirectives,
    CacheKey,
    cache_key,
)
from llm_gateway.cache.ports import (
    AnswerStore,
    AnswerStoreUnavailableError,
    Embedder,
    EmbedderUnavailableError,
    Neighbour,
)
from llm_gateway.chat.model import ChatRequest, CompletionChunk, Origin, Usage
from llm_gateway.chat.ports import ChunkStream
from llm_gateway.routing.breaker import CircuitBreaker

logger = logging.getLogger(__name__)

CACHE_PROVIDER = "cache"  # the Origin provider of a replayed answer
_FAILURES = (EmbedderUnavailableError, AnswerStoreUnavailableError, TimeoutError)


class LookupResult(StrEnum):
    HIT = "hit"
    MISS = "miss"
    INELIGIBLE = "ineligible"  # tools, temperature, images, or the client opted out
    UNAVAILABLE = "unavailable"  # an error, or the cache's circuit is open


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


class _Unobserved:
    def looked_up(self, result: LookupResult, score: float | None) -> None:
        pass


@dataclass(frozen=True)
class CacheSettings:
    similarity_threshold: float = 0.95
    ttl_s: float = 3600.0
    timeout_s: float = 1.0  # embed + search together; past it, the request goes on uncached


DEFAULT_CACHE_SETTINGS = CacheSettings()


class SemanticCache:
    def __init__(
        self,
        embedder: Embedder,
        store: AnswerStore,
        breaker: CircuitBreaker,
        settings: CacheSettings = DEFAULT_CACHE_SETTINGS,
        observer: CacheObserver | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._breaker = breaker
        self._settings = settings
        self._observer = observer or _Unobserved()
        self._clock = clock

    async def look_up(
        self, tenant_id: UUID, request: ChatRequest, directives: CacheDirectives
    ) -> Lookup:
        key = cache_key(tenant_id, request)
        if key is None or not (directives.read or directives.write):
            self._observer.looked_up(LookupResult.INELIGIBLE, None)
            return None
        found = await self._guarded(lambda: self._search(key, directives.read))
        if found is None:
            self._observer.looked_up(LookupResult.UNAVAILABLE, None)
            return None
        vector, neighbour = found
        if neighbour is not None:
            self._observer.looked_up(LookupResult.HIT, neighbour.score)
            return Hit(neighbour.answer, neighbour.score)
        self._observer.looked_up(LookupResult.MISS, None)
        return Miss(key, vector, store=directives.write)

    async def remember(self, miss: Miss, answer: CachedAnswer) -> None:
        """Keep a fresh answer. Never raises; only clean, bounded answers are kept."""
        if not miss.store or answer.finish_reason != "stop" or not answer.content:
            return
        if len(answer.content) > MAX_CACHED_ANSWER_CHARS:
            return
        expires_at = self._clock() + self._settings.ttl_s
        await self._guarded(lambda: self._store.put(miss.key, miss.vector, answer, expires_at))

    async def _search(
        self, key: CacheKey, read: bool
    ) -> tuple[tuple[float, ...], Neighbour | None]:
        vector = tuple(await self._embedder.embed(key.question))
        if not read:
            return vector, None
        neighbour = await self._store.nearest(
            key, vector, self._settings.similarity_threshold, self._clock()
        )
        return vector, neighbour

    async def _guarded[T](self, operation: Callable[[], Awaitable[T]]) -> T | None:
        """Run operation behind the breaker and the timeout; None when it couldn't."""
        permit = self._breaker.try_acquire()
        if permit is None:
            return None
        outcome_recorded = False
        try:
            with anyio.fail_after(self._settings.timeout_s):
                result = await operation()
            self._breaker.record_success()
            outcome_recorded = True
            return result
        except _FAILURES as error:
            logger.warning("semantic cache bypassed: %s", error or type(error).__name__)
            self._breaker.record_failure()
            outcome_recorded = True
            return None
        finally:
            if not outcome_recorded:  # cancelled: the call tells us nothing about health
                self._breaker.release(permit)


async def replay(hit: Hit) -> ChunkStream:
    """A cached answer as a provider stream: free (zero usage), marked as from the cache."""
    yield CompletionChunk(
        content=hit.answer.content, origin=Origin(CACHE_PROVIDER, hit.answer.model)
    )
    yield CompletionChunk(finish_reason=hit.answer.finish_reason)
    yield CompletionChunk(usage=Usage(0, 0))
