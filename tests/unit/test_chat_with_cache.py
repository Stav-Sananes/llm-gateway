"""The chat use case with the semantic cache in front of the provider."""

from uuid import UUID

import anyio
import pytest

from llm_gateway.cache.model import CacheDirectives
from llm_gateway.cache.semantic import CACHE_PROVIDER, CacheSettings, SemanticCache
from llm_gateway.chat.model import ChatRequest, CompletionChunk, Origin, Usage, collect
from llm_gateway.chat.ports import ProviderUnavailableError
from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.limits.ports import RateLimitedError
from llm_gateway.limits.token_bucket import InMemoryRateLimiter
from llm_gateway.tenancy.model import Tenant
from llm_gateway.usage.model import Outcome
from tests.support.cache import BagOfWordsEmbedder, InMemoryAnswerStore, breakers
from tests.support.limits import InMemoryTokenBudget
from tests.support.providers import ScriptedProvider
from tests.support.usage import RecordedUsage

pytestmark = pytest.mark.anyio

ACME = Tenant(UUID(int=1), "acme")
QUESTION = ChatRequest(
    "llama3", ({"role": "user", "content": "What is the capital of France?"},), temperature=0
)
ANSWER = (
    CompletionChunk(content="Par", origin=Origin("ollama", "llama3")),
    CompletionChunk(content="is."),
    CompletionChunk(finish_reason="stop"),
    CompletionChunk(usage=Usage(9, 3)),
)


class World:
    def __init__(
        self,
        provider: ScriptedProvider | None = None,
        store_down: bool = False,
        embedder: BagOfWordsEmbedder | None = None,
        timeout_s: float = 0.5,
        requests_allowed: int = 100,
    ) -> None:
        self.provider = provider or ScriptedProvider(ANSWER)
        self.store = InMemoryAnswerStore(down=store_down)
        self.budget = InMemoryTokenBudget(10_000)
        self.usage = RecordedUsage()
        self.cache = SemanticCache(
            embedder or BagOfWordsEmbedder(),
            self.store,
            breakers(threshold=5),
            CacheSettings(timeout_s=timeout_s),
        )
        self.chat = ChatCompletions(
            self.provider,
            # Refills too slowly to matter within a test: requests_allowed is the limit.
            InMemoryRateLimiter(capacity=requests_allowed, refill_per_s=0.001),
            self.budget,
            100,
            self.usage,
            cache=self.cache,
        )

    async def ask(
        self, request: ChatRequest = QUESTION, directives: CacheDirectives | None = None
    ) -> str:
        chunks = await self.chat.start(request, ACME, directives or CacheDirectives())
        content = (await collect(chunks)).content
        await self.cache.write_pending()  # what the app's background writer does
        return content


async def test_a_question_asked_again_is_answered_from_the_cache_for_free() -> None:
    world = World()

    first = await world.ask()
    again = await world.ask(
        ChatRequest(
            "llama3", ({"role": "user", "content": "what is the capital of france"},), temperature=0
        )
    )

    assert first == again == "Paris."
    assert len(world.provider.requests) == 1
    assert world.budget.settled == [12]  # the hit reserved and settled nothing
    assert world.budget.used(ACME.id) == 12
    hit = world.usage.events[1]
    assert (hit.provider, hit.model, hit.outcome) == (CACHE_PROVIDER, "llama3", Outcome.COMPLETED)


async def test_the_answer_is_kept_only_when_the_provider_finished() -> None:
    world = World(ScriptedProvider(ANSWER[:2], fail_after=True))

    with pytest.raises(ProviderUnavailableError):
        await world.ask()

    assert world.store.points == []


async def test_a_truncated_answer_is_not_kept() -> None:
    cut = (ANSWER[0], CompletionChunk(finish_reason="length"), ANSWER[3])
    world = World(ScriptedProvider(cut))

    await world.ask()

    assert world.store.points == []


async def test_no_cache_asks_the_provider_even_when_an_answer_is_kept() -> None:
    world = World()
    await world.ask()

    await world.ask(directives=CacheDirectives.from_header("no-cache"))

    assert len(world.provider.requests) == 2


async def test_with_the_cache_down_requests_go_to_the_provider() -> None:
    world = World(store_down=True)

    assert await world.ask() == "Paris."
    assert await world.ask() == "Paris."
    assert len(world.provider.requests) == 2


async def test_an_ineligible_request_neither_reads_nor_fills_the_cache() -> None:
    world = World()
    hot = ChatRequest("llama3", QUESTION.messages, temperature=0.9)

    await world.ask(hot)
    await world.ask(hot)

    assert len(world.provider.requests) == 2
    assert world.store.points == []


class NeverReturnsEmbedder(BagOfWordsEmbedder):
    async def embed(self, text: str) -> list[float]:
        await anyio.sleep_forever()
        raise AssertionError


async def test_a_request_cancelled_during_the_lookup_leaves_no_reservation() -> None:
    world = World(embedder=NeverReturnsEmbedder(), timeout_s=60)

    with anyio.move_on_after(0.05):
        await world.chat.start(QUESTION, ACME, CacheDirectives())

    assert world.budget.used(ACME.id) == 0


async def test_a_hit_is_served_even_when_the_budget_is_spent() -> None:
    world = World()
    await world.ask()
    await world.budget.reserve(ACME.id, 10_000 - world.budget.used(ACME.id))  # spend the rest

    assert await world.ask() == "Paris."


async def test_a_hit_still_counts_against_the_rate_limit() -> None:
    world = World(requests_allowed=2)
    await world.ask()  # a miss
    assert await world.ask() == "Paris."  # a hit
    assert len(world.provider.requests) == 1

    with pytest.raises(RateLimitedError):
        await world.ask()  # would be a hit, but the tenant is out of requests


async def test_an_answer_two_providers_stitched_together_is_not_kept() -> None:
    resumed = (
        CompletionChunk(content="Par", origin=Origin("claude", "opus")),
        CompletionChunk(content="is.", origin=Origin("ollama", "llama3")),  # took over
        CompletionChunk(finish_reason="stop"),
        CompletionChunk(usage=Usage(9, 3)),
    )
    world = World(ScriptedProvider(resumed))

    assert await world.ask() == "Paris."
    assert world.store.points == []
