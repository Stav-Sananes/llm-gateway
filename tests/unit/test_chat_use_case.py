from uuid import UUID

import pytest

from llm_gateway.chat.model import ChatRequest, CompletionChunk, Usage, collect
from llm_gateway.chat.ports import ProviderUnavailableError
from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.limits.estimate import estimate_prompt_tokens
from llm_gateway.limits.ports import BudgetExhaustedError, RateLimitedError
from llm_gateway.limits.token_bucket import InMemoryRateLimiter
from llm_gateway.tenancy.model import Tenant
from tests.support.limits import InMemoryTokenBudget, ScriptedProvider, SettleFailsBudget

pytestmark = pytest.mark.anyio

ACME = Tenant(id=UUID(int=1), name="acme")
DEFAULT_MAX_TOKENS = 100
REPLY = (
    CompletionChunk(content="Hi"),
    CompletionChunk(finish_reason="stop"),
    CompletionChunk(usage=Usage(prompt_tokens=7, completion_tokens=5)),
)


def a_request(max_tokens: int | None = None) -> ChatRequest:
    return ChatRequest(
        model="m", messages=({"role": "user", "content": "hello there"},), max_tokens=max_tokens
    )


def chat(
    provider: ScriptedProvider | None = None,
    budget: InMemoryTokenBudget | None = None,
    rate_limiter: InMemoryRateLimiter | None = None,
) -> ChatCompletions:
    return ChatCompletions(
        provider or ScriptedProvider(REPLY),
        rate_limiter or InMemoryRateLimiter(capacity=10, refill_per_s=1.0),
        budget or InMemoryTokenBudget(10_000),
        DEFAULT_MAX_TOKENS,
    )


async def test_rate_limited_request_never_reaches_the_provider() -> None:
    provider = ScriptedProvider(REPLY)
    limited = chat(provider, rate_limiter=InMemoryRateLimiter(capacity=0, refill_per_s=1.0))

    with pytest.raises(RateLimitedError):
        await limited.start(a_request(), ACME)

    assert provider.requests == []


async def test_request_the_budget_cannot_cover_is_refused_up_front() -> None:
    small = chat(budget=InMemoryTokenBudget(50))

    with pytest.raises(BudgetExhaustedError):
        await small.start(a_request(max_tokens=60), ACME)


async def test_reserves_prompt_estimate_plus_max_tokens() -> None:
    budget = InMemoryTokenBudget(10_000)
    request = a_request(max_tokens=60)

    await chat(budget=budget).start(request, ACME)  # reserved, not yet streamed

    assert budget.used(ACME.id) == estimate_prompt_tokens(request.messages) + 60


async def test_reserves_the_default_when_the_request_sets_no_max_tokens() -> None:
    budget = InMemoryTokenBudget(10_000)
    request = a_request()

    await chat(budget=budget).start(request, ACME)

    assert budget.used(ACME.id) == estimate_prompt_tokens(request.messages) + DEFAULT_MAX_TOKENS


async def test_finished_stream_is_charged_its_actual_usage() -> None:
    budget = InMemoryTokenBudget(10_000)

    await collect(await chat(budget=budget).start(a_request(max_tokens=60), ACME))

    assert budget.used(ACME.id) == 12


async def test_broken_stream_keeps_the_whole_reservation_charged() -> None:
    budget = InMemoryTokenBudget(10_000)
    provider = ScriptedProvider(REPLY[:1], fail_after=True)
    request = a_request(max_tokens=60)

    with pytest.raises(ProviderUnavailableError):
        await collect(await chat(provider, budget).start(request, ACME))

    assert budget.used(ACME.id) == estimate_prompt_tokens(request.messages) + 60


async def test_client_closing_the_stream_early_still_settles() -> None:
    budget = InMemoryTokenBudget(10_000)
    chunks = await chat(budget=budget).start(a_request(max_tokens=60), ACME)

    await anext(chunks)
    await chunks.aclose()

    assert budget.used(ACME.id) == estimate_prompt_tokens(a_request().messages) + 60


async def test_budget_store_failing_at_settle_does_not_break_the_response() -> None:
    completion = await collect(
        await chat(budget=SettleFailsBudget(10_000)).start(a_request(), ACME)
    )

    assert completion.content == "Hi"
