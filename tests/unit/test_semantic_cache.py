from typing import Any
from uuid import UUID

import anyio
import pytest

from llm_gateway.cache.model import CachedAnswer, CacheDirectives, cache_key
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
from llm_gateway.routing.breaker import CircuitBreaker
from tests.support.cache import BagOfWordsEmbedder, InMemoryAnswerStore

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

    def looked_up(self, result: LookupResult, score: float | None) -> None:
        self.results.append(result)


def a_cache(
    embedder: BagOfWordsEmbedder | None = None,
    store: InMemoryAnswerStore | None = None,
    now: list[float] | None = None,
    observer: Observer | None = None,
    **settings: Any,
) -> SemanticCache:
    clock = now if now is not None else [1000.0]
    return SemanticCache(
        embedder or BagOfWordsEmbedder(),
        store if store is not None else InMemoryAnswerStore(),
        CircuitBreaker("semantic-cache", failure_threshold=2, reset_after_s=30),
        CacheSettings(**({"timeout_s": 0.5} | settings)),
        observer=observer,
        clock=lambda: clock[0],
    )


async def cached(cache: SemanticCache, request: ChatRequest, tenant: UUID = ACME) -> None:
    lookup = await cache.look_up(tenant, request, CacheDirectives())
    assert isinstance(lookup, Miss)
    await cache.remember(lookup, ANSWER)


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

    lookup = await cache.look_up(ACME, asking(), CacheDirectives(read=False, write=True))
    assert isinstance(lookup, Miss)
    await cache.remember(lookup, ANSWER)

    assert len(store.points) == 2


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

    await cache.remember(lookup, answer)

    assert store.points == []


# Failures: the cache never fails a request


async def test_a_down_store_is_a_bypass_and_repeated_failures_open_the_circuit() -> None:
    embedder, observer = BagOfWordsEmbedder(), Observer()
    cache = a_cache(embedder, InMemoryAnswerStore(down=True), observer=observer)

    results = [await cache.look_up(ACME, asking(), CacheDirectives()) for _ in range(4)]

    assert results == [None] * 4
    assert len(embedder.calls) == 2  # threshold 2: then the open circuit skips the work
    assert observer.results == [LookupResult.UNAVAILABLE] * 4


async def test_a_slow_embedder_is_given_up_on_at_the_timeout() -> None:
    cache = a_cache(BagOfWordsEmbedder(delay_s=5), timeout_s=0.05)

    with anyio.fail_after(1):
        assert await cache.look_up(ACME, asking(), CacheDirectives()) is None


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
