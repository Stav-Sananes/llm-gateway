"""Ports for the semantic cache: turning text into a vector, and finding stored answers."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from llm_gateway.cache.model import CachedAnswer, CacheKey


class Embedder(Protocol):
    async def embed(self, text: str) -> Sequence[float]:
        """Raises EmbedderUnavailableError."""
        ...


@dataclass(frozen=True)
class Neighbour:
    answer: CachedAnswer
    score: float  # cosine similarity of the questions, 1.0 = same direction


class AnswerStore(Protocol):
    async def nearest(
        self, key: CacheKey, vector: Sequence[float], min_score: float, now: float
    ) -> Neighbour | None:
        """The closest unexpired answer with the key's tenant and context, if any scores at
        least min_score. Raises AnswerStoreUnavailableError."""
        ...

    async def put(
        self, key: CacheKey, vector: Sequence[float], answer: CachedAnswer, expires_at: float
    ) -> None:
        """Raises AnswerStoreUnavailableError."""
        ...


class EmbedderUnavailableError(Exception):
    """The embedding model could not be reached or answered badly."""


class AnswerStoreUnavailableError(Exception):
    """The vector store (Qdrant) could not be reached or answered badly."""
