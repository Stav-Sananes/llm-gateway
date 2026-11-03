"""The chat use case with the semantic cache in front of the provider."""

from uuid import UUID

import pytest

from llm_gateway.cache.model import CacheDirectives
from llm_gateway.cache.semantic import CACHE_PROVIDER, CacheSettings, SemanticCache
from llm_gateway.chat.model import ChatRequest, CompletionChunk, Origin, Usage, collect
from llm_gateway.chat.ports import ProviderUnavailableError
from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.limits.token_bucket import InMemoryRateLimiter
from llm_gateway.routing.breaker import CircuitBreaker
from llm_gateway.tenancy.model import Tenant
from llm_gateway.usage.model import Outcome
from tests.support.cache import BagOfWordsEmbedder, InMemoryAnswerStore
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
    def __init__(self, provider: ScriptedProvider | None = None, store_down: bool = False) -> None:
        self.provider = provider or ScriptedProvider(ANSWER)
        self.store = InMemoryAnswerStore(down=store_down)
        self.budget = InMemoryTokenBudget(10_000)
        self.usage = RecordedUsage()
        cache = SemanticCache(
            BagOfWordsEmbedder(),
            self.store,
            CircuitBreaker("semantic-cache", 5, 30),
            CacheSettings(timeout_s=0.5),
        )
        self.chat = ChatCompletions(
            self.provider,
            InMemoryRateLimiter(capacity=100, refill_per_s=100),
            self.budget,
            100,
            self.usage,
            cache=cache,
        )

    async def ask(
        self, request: ChatRequest = QUESTION, directives: CacheDirectives | None = None
    ) -> str:
        chunks = await self.chat.start(request, ACME, directives or CacheDirectives())
        return (await collect(chunks)).content


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
    assert world.budget.settled == [12, 0]  # the hit costs nothing
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
