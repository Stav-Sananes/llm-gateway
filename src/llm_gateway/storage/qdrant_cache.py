"""AnswerStore on Qdrant: one collection, every query filtered to one tenant and context.

Tenants share the collection but never each other's answers: every search filters on
tenant_id and context_hash (both indexed) before similarity is considered. Qdrant has no
TTL, so points carry expires_at: searches skip expired points, and every `sweep_every`
writes delete them.
"""

import time
from collections.abc import Callable, Sequence
from uuid import uuid4

import httpx
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from llm_gateway.cache.model import CachedAnswer, CacheKey
from llm_gateway.cache.ports import AnswerStoreUnavailableError, Neighbour

COLLECTION = "gw_semantic_cache"
_UNAVAILABLE = (UnexpectedResponse, ResponseHandlingException, httpx.HTTPError, OSError, ValueError)


class QdrantAnswerStore:
    def __init__(
        self,
        client: AsyncQdrantClient,
        dimensions: int,
        sweep_every: int = 100,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._dimensions = dimensions
        self._sweep_every = sweep_every
        self._clock = clock
        self._writes = 0
        self._ready = False

    async def nearest(
        self, key: CacheKey, vector: Sequence[float], min_score: float, now: float
    ) -> Neighbour | None:
        try:
            await self._ensure_collection()
            found = await self._client.query_points(
                COLLECTION,
                query=list(vector),
                query_filter=_same_question_space(key, now),
                score_threshold=min_score,
                limit=1,
                with_payload=True,
            )
        except _UNAVAILABLE as error:
            raise AnswerStoreUnavailableError(type(error).__name__) from error
        if not found.points:
            return None
        point = found.points[0]
        payload = point.payload or {}
        try:
            answer = CachedAnswer(
                str(payload["content"]), str(payload["finish_reason"]), str(payload["model"])
            )
        except KeyError as error:
            raise AnswerStoreUnavailableError(f"malformed point {point.id}") from error
        return Neighbour(answer, float(point.score))

    async def put(
        self, key: CacheKey, vector: Sequence[float], answer: CachedAnswer, expires_at: float
    ) -> None:
        point = models.PointStruct(
            id=str(uuid4()),
            vector=list(vector),
            payload={
                "tenant_id": str(key.tenant_id),
                "context_hash": key.context_hash,
                "content": answer.content,
                "finish_reason": answer.finish_reason,
                "model": answer.model,
                "expires_at": expires_at,
            },
        )
        try:
            await self._ensure_collection()
            await self._client.upsert(COLLECTION, points=[point], wait=False)
            self._writes += 1
            if self._writes % self._sweep_every == 0:
                await self._delete_expired()
        except _UNAVAILABLE as error:
            raise AnswerStoreUnavailableError(type(error).__name__) from error

    async def _delete_expired(self) -> None:
        expired = models.FieldCondition(key="expires_at", range=models.Range(lt=self._clock()))
        await self._client.delete(
            COLLECTION,
            points_selector=models.FilterSelector(filter=models.Filter(must=[expired])),
            wait=False,
        )

    async def _ensure_collection(self) -> None:
        """Created on first use, not at startup: the gateway must start without Qdrant."""
        if self._ready:
            return
        if not await self._client.collection_exists(COLLECTION):
            await self._client.create_collection(
                COLLECTION,
                vectors_config=models.VectorParams(
                    size=self._dimensions, distance=models.Distance.COSINE
                ),
            )
        else:
            await self._check_dimensions()
        for field, schema in (
            ("tenant_id", models.PayloadSchemaType.KEYWORD),
            ("context_hash", models.PayloadSchemaType.KEYWORD),
            ("expires_at", models.PayloadSchemaType.FLOAT),
        ):
            await self._client.create_payload_index(COLLECTION, field, field_schema=schema)
        self._ready = True

    async def _check_dimensions(self) -> None:
        # A collection left by another embedding model would fail every search.
        size = _vector_size(await self._client.get_collection(COLLECTION))
        if size != self._dimensions:
            raise AnswerStoreUnavailableError(
                f"{COLLECTION} holds {size}-dimension vectors; the embedder makes "
                f"{self._dimensions}. Drop the collection or fix GATEWAY_CACHE_EMBEDDING_DIMS."
            )


def _vector_size(info: models.CollectionInfo) -> int | None:
    vectors = info.config.params.vectors
    return vectors.size if isinstance(vectors, models.VectorParams) else None


def _same_question_space(key: CacheKey, now: float) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(
                key="tenant_id", match=models.MatchValue(value=str(key.tenant_id))
            ),
            models.FieldCondition(
                key="context_hash", match=models.MatchValue(value=key.context_hash)
            ),
            models.FieldCondition(key="expires_at", range=models.Range(gt=now)),
        ]
    )
