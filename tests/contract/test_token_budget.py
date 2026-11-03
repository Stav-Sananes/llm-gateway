"""Behaviour every TokenBudget must have: each test runs against the fake and real Redis."""

import asyncio
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import pytest

from llm_gateway.config import Settings
from llm_gateway.limits.ports import BudgetExhaustedError, TokenBudget
from llm_gateway.main import build_redis
from llm_gateway.storage.token_budget import RedisTokenBudget
from tests.support.limits import InMemoryTokenBudget

pytestmark = pytest.mark.anyio

BudgetFactory = Callable[[int], TokenBudget]


@pytest.fixture(params=["memory", "redis"])
async def budget_of(request: pytest.FixtureRequest) -> AsyncIterator[BudgetFactory]:
    if request.param == "memory":
        yield InMemoryTokenBudget
        return
    redis = build_redis(Settings(redis_url=request.getfixturevalue("redis_url")))
    try:
        await redis.flushdb()
        yield lambda tokens: RedisTokenBudget(redis, tokens)
    finally:
        await redis.aclose()


async def test_reservations_up_to_the_budget_succeed(budget_of: BudgetFactory) -> None:
    budget, tenant = budget_of(100), uuid4()

    await budget.reserve(tenant, 60)
    await budget.reserve(tenant, 40)

    with pytest.raises(BudgetExhaustedError):
        await budget.reserve(tenant, 1)


async def test_a_refused_reservation_takes_nothing(budget_of: BudgetFactory) -> None:
    budget, tenant = budget_of(100), uuid4()
    with pytest.raises(BudgetExhaustedError):
        await budget.reserve(tenant, 101)

    await budget.reserve(tenant, 100)


async def test_settling_for_less_refunds_the_difference(budget_of: BudgetFactory) -> None:
    budget, tenant = budget_of(100), uuid4()
    reservation = await budget.reserve(tenant, 80)

    await budget.settle(reservation, 30)

    await budget.reserve(tenant, 70)


async def test_settling_for_more_charges_the_difference(budget_of: BudgetFactory) -> None:
    budget, tenant = budget_of(100), uuid4()
    reservation = await budget.reserve(tenant, 50)

    await budget.settle(reservation, 90)

    with pytest.raises(BudgetExhaustedError):
        await budget.reserve(tenant, 11)


async def test_tenants_have_separate_budgets(budget_of: BudgetFactory) -> None:
    budget = budget_of(100)
    await budget.reserve(uuid4(), 100)

    await budget.reserve(uuid4(), 100)


async def test_concurrent_reservations_never_overspend(budget_of: BudgetFactory) -> None:
    budget, tenant = budget_of(100), uuid4()

    results = await asyncio.gather(
        *(budget.reserve(tenant, 10) for _ in range(25)), return_exceptions=True
    )

    assert sum(not isinstance(result, Exception) for result in results) == 10
