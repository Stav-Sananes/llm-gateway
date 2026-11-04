"""A request whose answer one provider began and another finished: metered per provider."""

import json
import logging
from typing import NoReturn
from uuid import UUID

import pytest

from llm_gateway.audit.chain import AuditEvent, AuditRecord
from llm_gateway.audit.log import AuditLog
from llm_gateway.audit.ports import AuditUnavailableError
from llm_gateway.audit.switches import SWITCHED, AuditedSwitches
from llm_gateway.chat.model import ChatRequest, CompletionChunk, Origin, Usage, collect
from llm_gateway.chat.ports import ProviderUnavailableError
from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.limits.estimate import estimate_prompt_tokens, estimate_text_tokens
from llm_gateway.limits.token_bucket import InMemoryRateLimiter
from llm_gateway.routing.router import Router, Target
from llm_gateway.tenancy.model import Tenant
from llm_gateway.usage.model import Outcome
from tests.support.in_memory_audit_store import InMemoryAuditStore
from tests.support.limits import InMemoryTokenBudget
from tests.support.providers import ScriptedProvider
from tests.support.usage import FakeClock, RecordedUsage

pytestmark = pytest.mark.anyio

ACME = Tenant(UUID(int=1), "acme")
QUESTION = ChatRequest("smart", ({"role": "user", "content": "What is the capital of France?"},))
PROMPT_ESTIMATE = estimate_prompt_tokens(QUESTION.messages)
MAX_TOKENS = 100
CLAUDE, LOCAL = Origin("claude", "opus"), Origin("local", "qwen")
LOCAL_USAGE = Usage(40, 5)


class RecordedSwitches:
    def __init__(self) -> None:
        self.switches: list[tuple[UUID, str, Origin, Origin]] = []

    async def record(
        self, tenant_id: UUID, requested_model: str, dead: Origin, resumed: Origin
    ) -> None:
        self.switches.append((tenant_id, requested_model, dead, resumed))


class World:
    def __init__(self, claude: ScriptedProvider, local: ScriptedProvider) -> None:
        self.budget = InMemoryTokenBudget(10_000)
        self.usage = RecordedUsage()
        self.switches = RecordedSwitches()
        router = Router(
            {"claude": claude, "local": local},
            {"smart": [Target("claude", "opus"), Target("local", "qwen")]},
            default_provider="local",
        )
        self.chat = ChatCompletions(
            router,
            InMemoryRateLimiter(capacity=10, refill_per_s=1.0),
            self.budget,
            MAX_TOKENS,
            self.usage,
            clock=FakeClock(step_s=1.0),
            switches=self.switches,
        )

    async def ask(self) -> str:
        return (await collect(await self.chat.start(QUESTION, ACME))).content


def dies_after(*texts: str) -> ScriptedProvider:
    return ScriptedProvider(tuple(CompletionChunk(content=t) for t in texts), fail_after=True)


def finishes() -> ScriptedProvider:
    return ScriptedProvider(
        (
            CompletionChunk(content=" is Paris."),
            CompletionChunk(finish_reason="stop"),
            CompletionChunk(usage=LOCAL_USAGE),
        )
    )


async def test_each_provider_that_served_the_answer_gets_its_own_usage_event() -> None:
    world = World(dies_after("The capital", " of France"), finishes())

    assert await world.ask() == "The capital of France is Paris."

    resumed, completed = world.usage.events
    estimated_output = estimate_text_tokens("The capital") + estimate_text_tokens(" of France")
    assert (resumed.provider, resumed.model, resumed.outcome) == ("claude", "opus", Outcome.RESUMED)
    assert (resumed.prompt_tokens, resumed.completion_tokens) == (PROMPT_ESTIMATE, estimated_output)
    assert resumed.charged_tokens == PROMPT_ESTIMATE + estimated_output
    assert (completed.provider, completed.outcome) == ("local", Outcome.COMPLETED)
    assert completed.charged_tokens == LOCAL_USAGE.total_tokens
    assert resumed.started_at == completed.started_at  # one request
    assert world.budget.settled == [resumed.charged_tokens + completed.charged_tokens]


async def test_the_switch_is_recorded() -> None:
    world = World(dies_after("The capital"), finishes())

    await world.ask()

    assert world.switches.switches == [(ACME.id, "smart", CLAUDE, LOCAL)]


async def test_an_answer_one_provider_served_records_no_switch() -> None:
    world = World(ScriptedProvider((CompletionChunk(content="Paris."),)), finishes())

    await world.ask()

    assert world.switches.switches == []
    assert [e.outcome for e in world.usage.events] == [Outcome.INTERRUPTED]  # no usage seen


async def test_a_resumed_answer_that_breaks_again_is_charged_its_whole_reservation() -> None:
    world = World(dies_after("The capital"), dies_after(" of France"))

    with pytest.raises(ProviderUnavailableError):
        await world.ask()

    assert [e.outcome for e in world.usage.events] == [Outcome.RESUMED, Outcome.INTERRUPTED]
    assert world.budget.settled == [PROMPT_ESTIMATE + MAX_TOKENS]


# The audit adapter


class DownAuditStore(InMemoryAuditStore):
    async def append(self, event: AuditEvent) -> NoReturn:
        raise AuditUnavailableError("postgres down")


async def test_a_switch_goes_on_the_tenants_audit_chain() -> None:
    store = InMemoryAuditStore()

    await AuditedSwitches(AuditLog(store)).record(ACME.id, "smart", CLAUDE, LOCAL)

    records: list[AuditRecord] = [r async for r in store.records(ACME.id, up_to_seq=10)]
    (record,) = records
    payload = json.loads(record.payload)
    assert payload["action"] == SWITCHED
    assert payload["details"] == {
        "requested_model": "smart",
        "from": {"provider": "claude", "model": "opus"},
        "to": {"provider": "local", "model": "qwen"},
    }


async def test_an_audit_store_that_is_down_costs_the_record_not_the_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        await AuditedSwitches(AuditLog(DownAuditStore())).record(ACME.id, "smart", CLAUDE, LOCAL)

    assert "provider switch not audited" in caplog.text
