"""The semantic cache's adapters: Qdrant for answers, an HTTP embeddings endpoint."""

import time
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast
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
    store: QdrantAnswerStore, key: CacheKey, min_score: float = 0.95, now: float = NOW
) -> Neighbour | None:
    for _ in range(50):  # wait=False writes become searchable within milliseconds
        found = await store.nearest(key, vector(key.question), min_score, now, limit=3)
        if found:
            return found[0]
        await anyio.sleep(0.02)
    return None


async def test_a_stored_answer_is_found_by_a_reworded_question(qdrant: AsyncQdrantClient) -> None:
    store = await stored(qdrant, KEY)

    found = await nearest(store, CacheKey(ACME, "ctx-1", "what is the capital of france"))

    assert found is not None and found.answer == ANSWER and found.score > 0.99
    assert found.question == KEY.question  # for the word-for-word guard


async def test_answers_never_cross_tenants_or_contexts(qdrant: AsyncQdrantClient) -> None:
    store = await stored(qdrant, KEY)
    assert await nearest(store, KEY) is not None  # positive control

    for other in (CacheKey(GLOBEX, "ctx-1", KEY.question), CacheKey(ACME, "ctx-2", KEY.question)):
        assert await store.nearest(other, vector(KEY.question), 0.95, NOW, limit=3) == []


async def test_a_question_below_the_threshold_is_not_an_answer(qdrant: AsyncQdrantClient) -> None:
    store = await stored(qdrant, KEY)
    assert await nearest(store, KEY) is not None

    other = CacheKey(ACME, "ctx-1", "What is the capital of Germany?")
    assert await store.nearest(other, vector(other.question), 0.95, NOW, limit=3) == []


async def test_an_expired_answer_is_not_returned_and_is_swept(qdrant: AsyncQdrantClient) -> None:
    store = await stored(qdrant, KEY, expires_at=NOW - 1)
    sweeper = QdrantAnswerStore(qdrant, DIMS, sweep_every=1, clock=lambda: NOW)
    assert await nearest(store, KEY, now=NOW - 2) is not None  # stored: visible before expiry

    assert await store.nearest(KEY, vector(KEY.question), 0.95, NOW, limit=3) == []
    fresh = CacheKey(ACME, "ctx-9", "fresh")
    await sweeper.put(fresh, vector(fresh.question), ANSWER, NOW + 60)
    for _ in range(50):
        if (await qdrant.count(COLLECTION, exact=True)).count == 1:
            break
        await anyio.sleep(0.02)

    assert (await qdrant.count(COLLECTION, exact=True)).count == 1
    assert await nearest(sweeper, fresh) is not None  # the sweep kept the fresh one
    assert await nearest(store, KEY, now=NOW - 2) is None  # and removed the expired one


async def test_a_collection_built_for_another_embedding_size_is_reported(
    qdrant: AsyncQdrantClient,
) -> None:
    await qdrant.create_collection(
        COLLECTION, vectors_config=models.VectorParams(size=8, distance=models.Distance.COSINE)
    )

    with pytest.raises(AnswerStoreUnavailableError, match="8-dimension"):
        await QdrantAnswerStore(qdrant, DIMS).nearest(KEY, vector(KEY.question), 0.95, NOW, limit=3)


async def test_qdrant_down_is_unavailable_quickly() -> None:
    down = AsyncQdrantClient(url=unused_local_url(), timeout=1, check_compatibility=False)
    started = time.monotonic()
    try:
        with pytest.raises(AnswerStoreUnavailableError):
            await QdrantAnswerStore(down, DIMS).nearest(
                KEY, vector(KEY.question), 0.95, NOW, limit=3
            )
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


async def test_the_same_question_stored_again_replaces_the_old_answer(
    qdrant: AsyncQdrantClient,
) -> None:
    store = await stored(qdrant, *[KEY] * 20)
    newer = CachedAnswer("Paris, France.", "stop", "llama3")
    await store.put(KEY, vector(KEY.question), newer, NOW + 60)

    for _ in range(50):
        found = await nearest(store, KEY)
        if found is not None and found.answer == newer:
            break
        await anyio.sleep(0.02)
    assert (await qdrant.count(COLLECTION, exact=True)).count == 1
    assert found is not None and found.answer == newer


async def test_concurrent_first_lookups_dont_race_to_create_the_collection(
    qdrant: AsyncQdrantClient,
) -> None:
    store = QdrantAnswerStore(qdrant, DIMS)
    errors: list[Exception] = []

    async def look() -> None:
        try:
            await store.nearest(KEY, vector(KEY.question), 0.95, NOW, limit=3)
        except AnswerStoreUnavailableError as error:
            errors.append(error)

    async with anyio.create_task_group() as tasks:
        for _ in range(8):
            tasks.start_soon(look)

    assert errors == []


async def test_another_replica_creating_the_collection_first_is_fine(
    qdrant: AsyncQdrantClient,
) -> None:
    # Two gateways, each with its own store object, both seeing "doesn't exist yet".
    first, second = QdrantAnswerStore(qdrant, DIMS), QdrantAnswerStore(qdrant, DIMS)

    async with anyio.create_task_group() as tasks:
        for store in (first, second, QdrantAnswerStore(qdrant, DIMS)):
            tasks.start_soon(store.nearest, KEY, vector(KEY.question), 0.95, NOW, 3)


class LateToCreate:
    """A client that, like a replica racing another, saw no collection that now exists."""

    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client

    async def collection_exists(self, name: str) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


@pytest.mark.parametrize(("existing_dims", "fine"), [(DIMS, True), (8, False)])
async def test_losing_the_race_to_create_the_collection_checks_the_winners_size(
    qdrant: AsyncQdrantClient, existing_dims: int, fine: bool
) -> None:
    await qdrant.create_collection(
        COLLECTION,
        vectors_config=models.VectorParams(size=existing_dims, distance=models.Distance.COSINE),
    )
    store = QdrantAnswerStore(cast(AsyncQdrantClient, LateToCreate(qdrant)), DIMS)

    if fine:
        assert await store.nearest(KEY, vector(KEY.question), 0.95, NOW, limit=3) == []
    else:
        with pytest.raises(AnswerStoreUnavailableError, match="8-dimension"):
            await store.nearest(KEY, vector(KEY.question), 0.95, NOW, limit=3)


async def test_a_collection_lost_with_its_qdrant_is_created_again(
    qdrant: AsyncQdrantClient,
) -> None:
    # Qdrant has no volume: when it restarts, it comes back empty.
    store = await stored(qdrant, KEY)
    await qdrant.delete_collection(COLLECTION)

    with pytest.raises(AnswerStoreUnavailableError):
        await store.nearest(KEY, vector(KEY.question), 0.95, NOW, limit=3)
    await store.put(KEY, vector(KEY.question), ANSWER, NOW + 60)
    await qdrant.count(COLLECTION)

    found = await nearest(store, KEY)
    assert found is not None and found.answer == ANSWER
