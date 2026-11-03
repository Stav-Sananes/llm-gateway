import logging
from uuid import UUID

import pytest

from llm_gateway.chat.model import ChatRequest, CompletionChunk, Usage, collect
from llm_gateway.chat.ports import ProviderUnavailableError
from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.limits.ports import BudgetExhaustedError, RateLimitedError
from llm_gateway.limits.token_bucket import InMemoryRateLimiter
from llm_gateway.tenancy.model import Tenant
from tests.support.limits import InMemoryTokenBudget, SettleFailsBudget
from tests.support.providers import ScriptedProvider

pytestmark = pytest.mark.anyio

ACME = Tenant(id=UUID(int=1), name="acme")
DEFAULT_MAX_TOKENS = 100
PROMPT = ({"role": "user", "content": "hello there"},)
PROMPT_ESTIMATE = 15  # see test_estimate.py for how prompts are estimated
USAGE = Usage(prompt_tokens=7, completion_tokens=5)
REPLY = (
    CompletionChunk(content="Hi"),
    CompletionChunk(finish_reason="stop"),
    CompletionChunk(usage=USAGE),
)


def a_request(max_tokens: int | None = None) -> ChatRequest:
    return ChatRequest(model="m", messages=PROMPT, max_tokens=max_tokens)


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


async def test_rate_limited_request_reserves_nothing_and_never_reaches_the_provider() -> None:
    provider, budget = ScriptedProvider(REPLY), InMemoryTokenBudget(10_000)
    limited = chat(provider, budget, InMemoryRateLimiter(capacity=1, refill_per_s=0.01))
    await collect(await limited.start(a_request(), ACME))
    spent = budget.used(ACME.id)

    with pytest.raises(RateLimitedError):
        await limited.start(a_request(), ACME)

    assert len(provider.requests) == 1
    assert budget.used(ACME.id) == spent


async def test_request_the_budget_cannot_cover_is_refused_up_front() -> None:
    provider = ScriptedProvider(REPLY)

    with pytest.raises(BudgetExhaustedError):
        await chat(provider, InMemoryTokenBudget(50)).start(a_request(max_tokens=60), ACME)

    assert provider.requests == []


async def test_reserves_prompt_estimate_plus_max_tokens() -> None:
    budget = InMemoryTokenBudget(10_000)

    await chat(budget=budget).start(a_request(max_tokens=60), ACME)  # reserved, not streamed

    assert budget.used(ACME.id) == PROMPT_ESTIMATE + 60


async def test_reserves_and_sends_the_default_when_the_request_sets_no_max_tokens() -> None:
    provider, budget = ScriptedProvider(REPLY), InMemoryTokenBudget(10_000)
    chunks = await chat(provider, budget).start(a_request(), ACME)

    assert budget.used(ACME.id) == PROMPT_ESTIMATE + DEFAULT_MAX_TOKENS
    await collect(chunks)
    [sent] = provider.requests
    assert sent.max_tokens == DEFAULT_MAX_TOKENS


async def test_finished_stream_is_charged_its_actual_usage() -> None:
    budget = InMemoryTokenBudget(10_000)

    await collect(await chat(budget=budget).start(a_request(max_tokens=60), ACME))

    assert budget.settled == [USAGE.total_tokens]
    assert budget.used(ACME.id) == USAGE.total_tokens


async def test_stream_broken_after_usage_is_charged_that_usage() -> None:
    budget = InMemoryTokenBudget(10_000)
    provider = ScriptedProvider(REPLY, fail_after=True)

    with pytest.raises(ProviderUnavailableError):
        await collect(await chat(provider, budget).start(a_request(max_tokens=60), ACME))

    assert budget.settled == [USAGE.total_tokens]


async def test_stream_broken_before_usage_keeps_the_whole_reservation_charged() -> None:
    budget = InMemoryTokenBudget(10_000)
    provider = ScriptedProvider(REPLY[:1], fail_after=True)

    with pytest.raises(ProviderUnavailableError):
        await collect(await chat(provider, budget).start(a_request(max_tokens=60), ACME))

    assert budget.settled == [PROMPT_ESTIMATE + 60]


async def test_provider_failing_before_any_output_charges_nothing() -> None:
    budget = InMemoryTokenBudget(10_000)
    provider = ScriptedProvider((), fail_after=True)

    with pytest.raises(ProviderUnavailableError):
        await collect(await chat(provider, budget).start(a_request(max_tokens=60), ACME))

    assert budget.settled == [0]
    assert budget.used(ACME.id) == 0


async def test_client_closing_early_settles_and_releases_the_upstream() -> None:
    provider, budget = ScriptedProvider(REPLY), InMemoryTokenBudget(10_000)
    chunks = await chat(provider, budget).start(a_request(max_tokens=60), ACME)

    await anext(chunks)
    await chunks.aclose()

    assert budget.settled == [PROMPT_ESTIMATE + 60]  # output started, no usage seen
    assert provider.closed


async def test_budget_store_failing_at_settle_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        completion = await collect(
            await chat(budget=SettleFailsBudget(10_000)).start(a_request(), ACME)
        )

    assert completion.content == "Hi"
    assert "reservation stays charged" in caplog.text
