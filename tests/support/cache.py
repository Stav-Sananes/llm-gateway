"""Fakes for the semantic cache's ports, behaving like the real adapters."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import anyio

from harness.fake_llm import bag_of_words
from llm_gateway.cache.model import CachedAnswer, CacheKey
from llm_gateway.cache.ports import (
    AnswerStoreUnavailableError,
    EmbedderUnavailableError,
    Neighbour,
)
from llm_gateway.cache.semantic import CacheBreakers
from llm_gateway.routing.breaker import CircuitBreaker

DIMS = 64


class BagOfWordsEmbedder:
    """The fake LLM's embedding, in process. `down` makes it fail; `delay_s` makes it slow."""

    def __init__(self, down: bool = False, delay_s: float = 0.0) -> None:
        self.down = down
        self.delay_s = delay_s
        self.calls: list[str] = []

    async def embed(self, text: str) -> Sequence[float]:
        self.calls.append(text)
        await anyio.sleep(self.delay_s)
        if self.down:
            raise EmbedderUnavailableError("embedder down")
        return bag_of_words(text, DIMS)


class ColdEmbedder(BagOfWordsEmbedder):
    """Fails its first `cold_calls` calls, like a model that is still loading."""

    def __init__(self, cold_calls: int) -> None:
        super().__init__()
        self.cold_calls = cold_calls

    async def embed(self, text: str) -> Sequence[float]:
        self.down = len(self.calls) < self.cold_calls
        return await super().embed(text)


@dataclass
class _Point:
    key: CacheKey
    vector: Sequence[float]
    answer: CachedAnswer
    expires_at: float


class InMemoryAnswerStore:
    """Cosine nearest neighbour, filtered to the key's tenant and context, like Qdrant."""

    def __init__(self, down: bool = False, delay_s: float = 0.0) -> None:
        self.points: list[_Point] = []
        self.down = down
        self.delay_s = delay_s

    async def nearest(
        self, key: CacheKey, vector: Sequence[float], min_score: float, now: float, limit: int
    ) -> Sequence[Neighbour]:
        await anyio.sleep(self.delay_s)
        if self.down:
            raise AnswerStoreUnavailableError("qdrant down")
        candidates = [
            Neighbour(p.answer, _cosine(vector, p.vector), p.key.question)
            for p in self.points
            if p.key.tenant_id == key.tenant_id
            and p.key.context_hash == key.context_hash
            and p.expires_at > now
        ]
        ranked = sorted(candidates, key=lambda n: n.score, reverse=True)
        return [n for n in ranked if n.score >= min_score][:limit]

    async def put(
        self, key: CacheKey, vector: Sequence[float], answer: CachedAnswer, expires_at: float
    ) -> None:
        await anyio.sleep(self.delay_s)
        if self.down:
            raise AnswerStoreUnavailableError("qdrant down")
        # Same tenant, context and question replace the old answer, as the point id does.
        self.points = [p for p in self.points if p.key != key]
        self.points.append(_Point(key, vector, answer, expires_at))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norms = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norms if norms else 0.0


class SameVectorEmbedder:
    """Gives every text the same vector: every stored question scores 1.0, as real
    embeddings do for texts that differ only past the model's context, or in one word."""

    async def embed(self, text: str) -> Sequence[float]:
        await anyio.lowlevel.checkpoint()
        return [1.0] + [0.0] * (DIMS - 1)


class MalformedEmbedder:
    """An embedder whose reply breaks in a way nobody planned for."""

    async def embed(self, text: str) -> Sequence[float]:
        raise RuntimeError("unexpected")


def breakers(threshold: int = 2) -> CacheBreakers:
    return CacheBreakers(
        embedder=CircuitBreaker("cache-embedder", threshold, reset_after_s=30),
        store=CircuitBreaker("cache-store", threshold, reset_after_s=30),
    )
