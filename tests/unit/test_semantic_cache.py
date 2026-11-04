import dataclasses
from typing import Any
from uuid import UUID

import anyio
import pytest

from llm_gateway.cache.model import (
    KEYED_REQUEST_FIELDS,
    CachedAnswer,
    CacheDirectives,
    cache_key,
)
from llm_gateway.cache.semantic import (
    CACHE_PROVIDER,
    CacheSettings,
    Hit,
    LookupResult,
    Miss,
    SemanticCache,
    replay,
)
from llm_gateway.chat.model import ChatRequest, Usage, collect
from tests.support.cache import (
    BagOfWordsEmbedder,
    ColdEmbedder,
    InMemoryAnswerStore,
    MalformedEmbedder,
    SameVectorEmbedder,
    breakers,
)

pytestmark = pytest.mark.anyio

ACME, GLOBEX = UUID(int=1), UUID(int=2)
ANSWER = CachedAnswer("Paris.", "stop", "llama3")


def asking(question: Any = "What is the capital of France?", **changes: Any) -> ChatRequest:
    fields: dict[str, Any] = {
        "model": "llama3",
        "messages": (
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": question},
        ),
        "temperature": 0.0,
    } | changes
    return ChatRequest(**fields)


class Observer:
    def __init__(self) -> None:
        self.results: list[LookupResult] = []
        self.dropped = 0

    def looked_up(self, result: LookupResult, score: float | None) -> None:
        self.results.append(result)

    def write_dropped(self) -> None:
        self.dropped += 1


def a_cache(
    embedder: BagOfWordsEmbedder | SameVectorEmbedder | MalformedEmbedder | None = None,
    store: InMemoryAnswerStore | None = None,
    now: list[float] | None = None,
    observer: Observer | None = None,
    **settings: Any,
) -> SemanticCache:
    clock = now if now is not None else [1000.0]
    return SemanticCache(
        embedder or BagOfWordsEmbedder(),
        store if store is not None else InMemoryAnswerStore(),
        breakers(threshold=2),
        CacheSettings(**({"timeout_s": 0.5} | settings)),
        observer=observer,
        clock=lambda: clock[0],
    )


async def cached(cache: SemanticCache, request: ChatRequest, tenant: UUID = ACME) -> None:
    lookup = await cache.look_up(tenant, request, CacheDirectives())
    assert isinstance(lookup, Miss)
    cache.remember(lookup, ANSWER)
    await cache.write_pending()


# Which requests may use the cache at all


@pytest.mark.parametrize(
    "request_",
    [
        asking(temperature=None),  # the provider's default: sampling
        asking(temperature=0.5),
        asking(has_tools=True),
        asking([{"type": "text", "text": "this"}, {"type": "image_url", "image_url": {}}]),
        asking("   "),
        ChatRequest(
            "m",
            ({"role": "user", "content": "hi"}, {"role": "assistant", "content": "x"}),
            temperature=0,
        ),
        ChatRequest(
            "m",
            ({"role": "tool", "content": "42"}, {"role": "user", "content": "and?"}),
            temperature=0,
        ),
    ],
    ids=["no-temperature", "hot", "tools", "image", "blank", "last-not-user", "tool-message"],
)
def test_requests_that_must_not_use_the_cache_have_no_key(request_: ChatRequest) -> None:
    assert cache_key(ACME, request_) is None


def test_the_question_is_matched_by_meaning_and_the_rest_exactly() -> None:
    key = cache_key(ACME, asking())
    assert key is not None and key.question == "What is the capital of France?"

    assert cache_key(ACME, asking("different question")).context_hash == key.context_hash  # type: ignore[union-attr]
    for other in (
        asking(model="llama3:70b"),
        asking(max_tokens=10),
        ChatRequest("llama3", (asking().messages[1],), temperature=0.0),  # no system prompt
    ):
        assert cache_key(ACME, other).context_hash != key.context_hash  # type: ignore[union-attr]


def test_text_parts_count_as_text() -> None:
    key = cache_key(
        ACME, asking([{"type": "text", "text": "What is"}, {"type": "text", "text": "it?"}])
    )

    assert key is not None and key.question == "What is\nit?"


@pytest.mark.parametrize(
    ("header", "read", "write"),
    [
        (None, True, True),
        ("no-cache", False, True),
        ("No-Store", False, False),
        ("max-age=0, no-cache", False, True),
    ],
)
def test_cache_control_directives(header: str | None, read: bool, write: bool) -> None:
    assert CacheDirectives.from_header(header) == CacheDirectives(read=read, write=write)


# Lookups


async def test_the_same_question_reworded_is_a_hit() -> None:
    cache = a_cache()
    await cached(cache, asking())

    lookup = await cache.look_up(ACME, asking("what is the capital of france"), CacheDirectives())

    assert isinstance(lookup, Hit) and lookup.answer == ANSWER
    assert lookup.score > 0.99


async def test_a_different_question_is_a_miss() -> None:
    cache = a_cache()
    await cached(cache, asking())

    lookup = await cache.look_up(ACME, asking("What is the capital of Germany?"), CacheDirectives())

    assert isinstance(lookup, Miss)


async def test_another_tenants_answer_is_never_used() -> None:
    cache = a_cache()
    await cached(cache, asking(), tenant=GLOBEX)

    assert isinstance(await cache.look_up(ACME, asking(), CacheDirectives()), Miss)


async def test_an_answer_expires_after_its_ttl() -> None:
    now = [1000.0]
    cache = a_cache(now=now, ttl_s=60)
    await cached(cache, asking())
    now[0] += 61

    assert isinstance(await cache.look_up(ACME, asking(), CacheDirectives()), Miss)


async def test_no_cache_skips_the_lookup_but_keeps_the_fresh_answer() -> None:
    store = InMemoryAnswerStore()
    cache = a_cache(store=store)
    await cached(cache, asking())

    store.points[0].answer = CachedAnswer("stale", "stop", "llama3")

    lookup = await cache.look_up(ACME, asking(), CacheDirectives(read=False, write=True))
    assert isinstance(lookup, Miss)
    cache.remember(lookup, ANSWER)
    await cache.write_pending()

    assert [p.answer for p in store.points] == [ANSWER]  # the fresh answer replaced it


async def test_no_store_bypasses_the_cache_entirely() -> None:
    embedder = BagOfWordsEmbedder()

    lookup = await a_cache(embedder).look_up(
        ACME, asking(), CacheDirectives.from_header("no-store")
    )

    assert lookup is None and embedder.calls == []


@pytest.mark.parametrize(
    "answer",
    [
        CachedAnswer("cut off", "length", "m"),
        CachedAnswer("", "stop", "m"),
        CachedAnswer("x" * 70_000, "stop", "m"),
    ],
    ids=["truncated", "empty", "too-long"],
)
async def test_only_clean_bounded_answers_are_remembered(answer: CachedAnswer) -> None:
    store = InMemoryAnswerStore()
    cache = a_cache(store=store)
    lookup = await cache.look_up(ACME, asking(), CacheDirectives())
    assert isinstance(lookup, Miss)

    cache.remember(lookup, answer)
    await cache.write_pending()

    assert store.points == []


# Failures: the cache never fails a request


async def test_a_down_store_is_a_bypass_and_repeated_failures_open_the_circuit() -> None:
    embedder, observer = BagOfWordsEmbedder(), Observer()
    cache = a_cache(embedder, InMemoryAnswerStore(down=True), observer=observer)

    results = [await cache.look_up(ACME, asking(), CacheDirectives()) for _ in range(4)]

    assert results == [None] * 4
    assert len(embedder.calls) == 2  # threshold 2: then the store's open circuit skips it all
    assert observer.results == [LookupResult.UNAVAILABLE] * 4


async def test_a_slow_embedder_is_given_up_on_at_the_timeout() -> None:
    cache = a_cache(BagOfWordsEmbedder(delay_s=5), timeout_s=0.05)

    with anyio.fail_after(1):
        assert await cache.look_up(ACME, asking(), CacheDirectives()) is None


async def test_warming_up_retries_a_cold_embedder_without_opening_its_circuit() -> None:
    embedder = ColdEmbedder(cold_calls=3)  # more failures than the breaker's threshold
    cache = a_cache(embedder)

    with anyio.fail_after(1):
        assert await cache.warm_up(within_s=1, every_s=0)

    assert len(embedder.calls) == 4
    assert isinstance(await cache.look_up(ACME, asking(), CacheDirectives()), Miss)


async def test_warming_up_gives_up_on_an_embedder_that_never_answers() -> None:
    cache = a_cache(BagOfWordsEmbedder(down=True))

    with anyio.fail_after(1):
        assert not await cache.warm_up(within_s=0.05, every_s=0.01)


async def test_lookups_are_observed_by_result() -> None:
    observer = Observer()
    cache = a_cache(observer=observer)

    await cached(cache, asking())
    await cache.look_up(ACME, asking(), CacheDirectives())
    await cache.look_up(ACME, asking(temperature=1.0), CacheDirectives())

    assert observer.results == [LookupResult.MISS, LookupResult.HIT, LookupResult.INELIGIBLE]


async def test_a_hit_replays_as_a_free_stream_marked_as_from_the_cache() -> None:
    chunks = [chunk async for chunk in replay(Hit(ANSWER, 0.99))]

    assert chunks[0].origin is not None and chunks[0].origin.provider == CACHE_PROVIDER
    completion = await collect(replay(Hit(ANSWER, 0.99)))
    assert (completion.content, completion.finish_reason, completion.usage) == (
        "Paris.",
        "stop",
        Usage(0, 0),
    )


async def test_a_question_differing_in_a_negation_is_not_a_hit_even_at_score_1() -> None:
    cache = a_cache(SameVectorEmbedder())  # every question scores 1.0
    await cached(cache, asking("Is it safe to mix bleach and ammonia?"))

    lookup = await cache.look_up(
        ACME, asking("Is it unsafe to mix bleach and ammonia?"), CacheDirectives()
    )

    assert isinstance(lookup, Miss)


async def test_a_question_differing_in_a_number_is_not_a_hit_even_at_score_1() -> None:
    cache = a_cache(SameVectorEmbedder())
    await cached(cache, asking("How much is 2 plus 2?"))

    assert isinstance(
        await cache.look_up(ACME, asking("How much is 2 plus 3?"), CacheDirectives()), Miss
    )


async def test_a_long_question_matches_only_word_for_word() -> None:
    # Past the embedding model's context, a shared document hides the instruction after it.
    document = "word " * 300
    cache = a_cache(SameVectorEmbedder())
    await cached(cache, asking(document + "Answer: YES"))

    other = await cache.look_up(
        ACME, asking(document + "Answer: NO, and explain why"), CacheDirectives()
    )
    same = await cache.look_up(ACME, asking(document + "Answer: YES"), CacheDirectives())

    assert isinstance(other, Miss)
    assert isinstance(same, Hit)


async def test_a_malformed_embedder_reply_is_a_bypass_not_an_error() -> None:
    assert await a_cache(MalformedEmbedder()).look_up(ACME, asking(), CacheDirectives()) is None


async def test_a_hung_store_opens_its_own_circuit_even_when_embedding_works() -> None:
    store = InMemoryAnswerStore(delay_s=5)
    cache = a_cache(store=store, timeout_s=0.02)
    write_only = CacheDirectives(read=False, write=True)  # embeds fine, then writes time out

    for _ in range(3):
        lookup = await cache.look_up(ACME, asking(), write_only)
        if isinstance(lookup, Miss):
            cache.remember(lookup, ANSWER)
            await cache.write_pending()

    assert await cache.look_up(ACME, asking(), CacheDirectives()) is None  # circuit open


async def test_remembering_never_waits_and_a_full_queue_drops() -> None:
    observer, store = Observer(), InMemoryAnswerStore()
    cache = a_cache(store=store, observer=observer, write_queue=1)
    lookup = await cache.look_up(ACME, asking(), CacheDirectives())
    assert isinstance(lookup, Miss)
    store.delay_s = 5  # Qdrant hangs from here on

    cache.remember(lookup, ANSWER)  # sync: it can't wait on the hung store
    cache.remember(lookup, ANSWER)

    assert observer.dropped == 1


async def test_the_writer_stores_queued_answers_in_the_background() -> None:
    store = InMemoryAnswerStore()
    cache = a_cache(store=store)
    lookup = await cache.look_up(ACME, asking(), CacheDirectives())
    assert isinstance(lookup, Miss)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(cache.run_writer)
        cache.remember(lookup, ANSWER)
        with anyio.fail_after(1):
            while not store.points:
                await anyio.lowlevel.checkpoint()
        tasks.cancel_scope.cancel()

    assert [p.answer for p in store.points] == [ANSWER]


def test_every_request_field_that_changes_the_answer_is_keyed() -> None:
    # A new ChatRequest field must be added to the hash, or excluded here on purpose.
    fields = {f.name for f in dataclasses.fields(ChatRequest)} - {"messages"}

    assert fields == KEYED_REQUEST_FIELDS


@pytest.mark.parametrize(
    "other",
    [
        asking(temperature=0.3),
        ChatRequest(
            "llama3",
            (asking().messages[0], {**asking().messages[1], "name": "bob"}),
            temperature=0.0,
        ),
    ],
    ids=["temperature", "last-message-name"],
)
def test_fields_beside_the_question_text_split_the_key(other: ChatRequest) -> None:
    assert cache_key(ACME, other).context_hash != cache_key(ACME, asking()).context_hash  # type: ignore[union-attr]


def test_an_image_anywhere_in_the_conversation_makes_it_uncacheable() -> None:
    earlier_image = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
    request = ChatRequest(
        "llama3", (earlier_image, {"role": "user", "content": "And now?"}), temperature=0
    )

    assert cache_key(ACME, request) is None
