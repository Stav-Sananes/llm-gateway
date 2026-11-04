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
    question: str  # the question the answer was for


class AnswerStore(Protocol):
    async def nearest(
        self, key: CacheKey, vector: Sequence[float], min_score: float, now: float, limit: int
    ) -> Sequence[Neighbour]:
        """Up to `limit` unexpired answers with the key's tenant and context scoring at least
        min_score, best first. Raises AnswerStoreUnavailableError."""
        ...

    async def put(
        self, key: CacheKey, vector: Sequence[float], answer: CachedAnswer, expires_at: float
    ) -> None:
        """Replaces the answer stored for the same tenant, context and question, if any.
        Raises AnswerStoreUnavailableError."""
        ...


class EmbedderUnavailableError(Exception):
    """The embedding model could not be reached or answered badly."""


class AnswerStoreUnavailableError(Exception):
    """The vector store (Qdrant) could not be reached or answered badly."""
