"""The semantic cache's adapters: Qdrant for answers, an HTTP embeddings endpoint."""

import time
from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import anyio
import httpx
import pytest
from qdrant_client import AsyncQdrantClient, models

from harness.fake_llm import FakeLlmScript, bag_of_words, create_fake_llm
from llm_gateway.cache.model import CachedAnswer, CacheKey
from llm_gateway.cache.ports import (
    AnswerStoreUnavailableError,
    EmbedderUnavailableError,
    Neighbour,
)
from llm_gateway.providers.embeddings import OpenAICompatibleEmbedder
from llm_gateway.storage.qdrant_cache import COLLECTION, QdrantAnswerStore
from tests.support.servers import serving, unused_local_url

pytestmark = pytest.mark.anyio

DIMS = 64
ACME, GLOBEX = UUID(int=1), UUID(int=2)
KEY = CacheKey(ACME, "ctx-1", "What is the capital of France?")
ANSWER = CachedAnswer("Paris.", "stop", "llama3")
NOW = 1_000_000.0


@pytest.fixture
async def qdrant(qdrant_url: str) -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(url=qdrant_url, timeout=5, check_compatibility=False)
    if await client.collection_exists(COLLECTION):
        await client.delete_collection(COLLECTION)  # clean at the start, not the end
    yield client
    await client.close()


def vector(text: str) -> list[float]:
    return bag_of_words(text, DIMS)


async def stored(
    qdrant: AsyncQdrantClient, *keys: CacheKey, expires_at: float = NOW + 60
) -> QdrantAnswerStore:
    store = QdrantAnswerStore(qdrant, DIMS, clock=lambda: NOW)
    for key in keys:
        await store.put(key, vector(key.question), ANSWER, expires_at)
    await qdrant.count(COLLECTION)  # puts don't wait; make the writes visible
    return store


async def nearest(
    store: QdrantAnswerStore, key: CacheKey, min_score: float = 0.95
) -> Neighbour | None:
    for _ in range(50):  # wait=False writes become searchable within milliseconds
        found = await store.nearest(key, vector(key.question), min_score, NOW)
        if found is not None:
            return found
        await anyio.sleep(0.02)
    return None


async def test_a_stored_answer_is_found_by_a_reworded_question(qdrant: AsyncQdrantClient) -> None:
    store = await stored(qdrant, KEY)

    found = await nearest(store, CacheKey(ACME, "ctx-1", "what is the capital of france"))

    assert found is not None and found.answer == ANSWER and found.score > 0.99


async def test_answers_never_cross_tenants_or_contexts(qdrant: AsyncQdrantClient) -> None:
    store = await stored(qdrant, KEY)
    assert await nearest(store, KEY) is not None  # positive control

    assert (
        await store.nearest(
            CacheKey(GLOBEX, "ctx-1", KEY.question), vector(KEY.question), 0.95, NOW
        )
        is None
    )
    assert (
        await store.nearest(CacheKey(ACME, "ctx-2", KEY.question), vector(KEY.question), 0.95, NOW)
        is None
    )


async def test_a_question_below_the_threshold_is_not_an_answer(qdrant: AsyncQdrantClient) -> None:
    store = await stored(qdrant, KEY)
    assert await nearest(store, KEY) is not None

    other = CacheKey(ACME, "ctx-1", "What is the capital of Germany?")
    assert await store.nearest(other, vector(other.question), 0.95, NOW) is None


async def test_an_expired_answer_is_not_returned_and_is_swept(qdrant: AsyncQdrantClient) -> None:
    store = await stored(qdrant, KEY, expires_at=NOW - 1)
    sweeper = QdrantAnswerStore(qdrant, DIMS, sweep_every=1, clock=lambda: NOW)

    assert await store.nearest(KEY, vector(KEY.question), 0.95, NOW) is None
    await sweeper.put(CacheKey(ACME, "ctx-9", "fresh"), vector("fresh"), ANSWER, NOW + 60)
    for _ in range(50):
        if (await qdrant.count(COLLECTION, exact=True)).count == 1:
            break
        await anyio.sleep(0.02)

    assert (await qdrant.count(COLLECTION, exact=True)).count == 1  # only the fresh one


async def test_a_collection_built_for_another_embedding_size_is_reported(
    qdrant: AsyncQdrantClient,
) -> None:
    await qdrant.create_collection(
        COLLECTION, vectors_config=models.VectorParams(size=8, distance=models.Distance.COSINE)
    )

    with pytest.raises(AnswerStoreUnavailableError, match="8-dimension"):
        await QdrantAnswerStore(qdrant, DIMS).nearest(KEY, vector(KEY.question), 0.95, NOW)


async def test_qdrant_down_is_unavailable_quickly() -> None:
    down = AsyncQdrantClient(url=unused_local_url(), timeout=1, check_compatibility=False)
    started = time.monotonic()
    try:
        with pytest.raises(AnswerStoreUnavailableError):
            await QdrantAnswerStore(down, DIMS).nearest(KEY, vector(KEY.question), 0.95, NOW)
    finally:
        await down.close()

    assert time.monotonic() - started < 3


@pytest.fixture
def embeddings_url() -> Iterator[str]:
    with serving(create_fake_llm(FakeLlmScript(embedding_dims=DIMS))) as url:
        yield f"{url}/v1"


async def test_the_embedder_returns_the_models_vector(embeddings_url: str) -> None:
    async with httpx.AsyncClient(base_url=embeddings_url) as client:
        got = await OpenAICompatibleEmbedder(client, "nomic-embed-text", DIMS).embed("Hello there")

    assert list(got) == vector("Hello there")


async def test_an_embedding_of_the_wrong_size_is_refused(embeddings_url: str) -> None:
    async with httpx.AsyncClient(base_url=embeddings_url) as client:
        with pytest.raises(EmbedderUnavailableError, match="768"):
            await OpenAICompatibleEmbedder(client, "m", 768).embed("Hello there")


async def test_an_embedder_error_or_outage_is_unavailable() -> None:
    with serving(create_fake_llm(FakeLlmScript(embed_fail_status=500))) as url:
        async with httpx.AsyncClient(base_url=f"{url}/v1") as client:
            with pytest.raises(EmbedderUnavailableError):
                await OpenAICompatibleEmbedder(client, "m", DIMS).embed("hi")
    async with httpx.AsyncClient(base_url=unused_local_url()) as client:
        with pytest.raises(EmbedderUnavailableError):
            await OpenAICompatibleEmbedder(client, "m", DIMS).embed("hi")
