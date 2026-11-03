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


@dataclass
class _Point:
    key: CacheKey
    vector: Sequence[float]
    answer: CachedAnswer
    expires_at: float


class InMemoryAnswerStore:
    """Cosine nearest neighbour, filtered to the key's tenant and context, like Qdrant."""

    def __init__(self, down: bool = False) -> None:
        self.points: list[_Point] = []
        self.down = down

    async def nearest(
        self, key: CacheKey, vector: Sequence[float], min_score: float, now: float
    ) -> Neighbour | None:
        await anyio.lowlevel.checkpoint()
        if self.down:
            raise AnswerStoreUnavailableError("qdrant down")
        candidates = [
            Neighbour(p.answer, _cosine(vector, p.vector))
            for p in self.points
            if p.key.tenant_id == key.tenant_id
            and p.key.context_hash == key.context_hash
            and p.expires_at > now
        ]
        best = max(candidates, key=lambda n: n.score, default=None)
        return best if best is not None and best.score >= min_score else None

    async def put(
        self, key: CacheKey, vector: Sequence[float], answer: CachedAnswer, expires_at: float
    ) -> None:
        await anyio.lowlevel.checkpoint()
        if self.down:
            raise AnswerStoreUnavailableError("qdrant down")
        self.points.append(_Point(key, vector, answer, expires_at))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norms = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norms if norms else 0.0
