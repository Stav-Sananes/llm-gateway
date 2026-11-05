import asyncio
import time

import pytest

from llm_gateway.chat.model import ChatRequest, CompletionChunk, collect
from llm_gateway.chat.ports import (
    ChunkStream,
    GatewayBusyError,
    ProviderRejectedError,
    ProviderUnavailableError,
)
from llm_gateway.routing.breaker import BreakerState, CircuitBreaker, GuardedProvider, Permit
from tests.support.providers import ScriptedProvider

pytestmark = pytest.mark.anyio

REQUEST = ChatRequest(model="m", messages=({"role": "user", "content": "hi"},))
OK = (CompletionChunk(content="ok"),)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def a_breaker(clock: FakeClock, threshold: int = 2, reset_after_s: float = 10.0) -> CircuitBreaker:
    return CircuitBreaker("p", threshold, reset_after_s, clock)


def test_opens_after_the_threshold_of_failures_in_a_row() -> None:
    breaker = a_breaker(FakeClock())
    breaker.record_failure()
    assert breaker.try_acquire() is Permit.NORMAL  # one failure: still closed

    breaker.record_failure()

    assert breaker.state is BreakerState.OPEN
    assert breaker.try_acquire() is None


def test_a_success_resets_the_failure_count() -> None:
    breaker = a_breaker(FakeClock())
    breaker.record_failure()
    breaker.record_success()

    breaker.record_failure()

    assert breaker.state is BreakerState.CLOSED


def test_half_open_after_the_reset_time_lets_one_trial_through() -> None:
    clock = FakeClock()
    breaker = a_breaker(clock)
    breaker.record_failure()
    breaker.record_failure()

    clock.now += 10

    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.try_acquire() is Permit.TRIAL
    assert breaker.try_acquire() is None  # one trial at a time


def test_a_successful_trial_closes_the_circuit() -> None:
    clock = FakeClock()
    breaker = a_breaker(clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.now += 10
    breaker.try_acquire()

    breaker.record_success()

    assert breaker.state is BreakerState.CLOSED


def test_a_failed_trial_reopens_and_restarts_the_wait() -> None:
    clock = FakeClock()
    breaker = a_breaker(clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.now += 10
    breaker.try_acquire()

    breaker.record_failure()

    assert breaker.state is BreakerState.OPEN
    clock.now += 9.9
    assert breaker.state is BreakerState.OPEN


async def test_guarded_provider_refuses_without_calling_an_open_provider() -> None:
    clock = FakeClock()
    breaker = a_breaker(clock, threshold=1)
    breaker.record_failure()
    provider = ScriptedProvider(OK)

    with pytest.raises(ProviderUnavailableError, match="circuit is open"):
        await collect(GuardedProvider(provider, breaker).stream(REQUEST))

    assert provider.requests == []


async def test_guarded_provider_counts_unavailability_as_failure() -> None:
    breaker = a_breaker(FakeClock(), threshold=1)
    guarded = GuardedProvider(ScriptedProvider((), fail_after=True), breaker)

    with pytest.raises(ProviderUnavailableError):
        await collect(guarded.stream(REQUEST))

    assert breaker.state is BreakerState.OPEN


async def test_guarded_provider_does_not_count_a_rejected_request_against_the_provider() -> None:
    breaker = a_breaker(FakeClock(), threshold=1)
    guarded = GuardedProvider(ScriptedProvider(OK, rejects=True), breaker)

    with pytest.raises(ProviderRejectedError):
        await collect(guarded.stream(REQUEST))

    assert breaker.state is BreakerState.CLOSED


class SlowProvider:
    """Never produces its first chunk: the caller gives up first."""

    async def stream(self, request: ChatRequest) -> ChunkStream:
        await asyncio.sleep(60)
        yield CompletionChunk(content="too late")


async def test_a_trial_abandoned_before_any_output_frees_the_next_trial() -> None:
    clock = FakeClock()
    breaker = a_breaker(clock, threshold=1)
    breaker.record_failure()
    clock.now += 10
    trial = GuardedProvider(SlowProvider(), breaker).stream(REQUEST)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(trial), timeout=0.05)  # the client left
    await trial.aclose()

    assert breaker.try_acquire() is Permit.TRIAL


def test_an_older_call_ending_does_not_free_the_running_trial() -> None:
    clock = FakeClock()
    breaker = a_breaker(clock, threshold=1)
    older = breaker.try_acquire()  # started while closed
    breaker.record_failure()
    clock.now += 10
    assert breaker.try_acquire() is Permit.TRIAL
    assert older is not None

    breaker.release(older)  # the older call ends without an answer

    assert breaker.try_acquire() is None


async def test_a_trial_that_streams_closes_the_circuit() -> None:
    clock = FakeClock()
    breaker = a_breaker(clock, threshold=1)
    breaker.record_failure()
    clock.now += 10

    await collect(GuardedProvider(ScriptedProvider(OK), breaker).stream(REQUEST))

    assert breaker.state is BreakerState.CLOSED


async def test_a_provider_failing_after_its_first_chunk_still_counts_as_a_failure() -> None:
    breaker = a_breaker(FakeClock(), threshold=1)
    guarded = GuardedProvider(ScriptedProvider(OK, fail_after=True), breaker)

    with pytest.raises(ProviderUnavailableError):
        await collect(guarded.stream(REQUEST))

    assert breaker.state is BreakerState.OPEN


async def test_a_provider_that_keeps_dying_mid_answer_opens_its_circuit() -> None:
    # Each call streams before it dies: that first chunk must not count as a success,
    # or the failures never add up (and resume would hide them from every client).
    breaker = a_breaker(FakeClock(), threshold=2)
    guarded = GuardedProvider(ScriptedProvider(OK, fail_after=True), breaker)

    for _ in range(2):
        with pytest.raises(ProviderUnavailableError):
            await collect(guarded.stream(REQUEST))

    assert breaker.state is BreakerState.OPEN


async def test_a_trial_whose_client_leaves_mid_answer_frees_the_next_trial() -> None:
    clock = FakeClock()
    breaker = a_breaker(clock, threshold=1)
    breaker.record_failure()
    clock.now += 10
    trial = GuardedProvider(ScriptedProvider(OK + OK), breaker).stream(REQUEST)

    await anext(trial)
    await trial.aclose()  # the client left: the trial told us nothing

    assert breaker.try_acquire() is Permit.TRIAL


async def test_the_gateways_own_saturation_does_not_count_against_the_provider() -> None:
    class Busy:
        async def stream(self, request: ChatRequest) -> ChunkStream:
            raise GatewayBusyError("no free connection")
            yield  # pragma: no cover - makes this a generator

    breaker = a_breaker(FakeClock(), threshold=1)

    for _ in range(3):
        with pytest.raises(GatewayBusyError):
            await collect(GuardedProvider(Busy(), breaker).stream(REQUEST))

    assert breaker.state is BreakerState.CLOSED


async def test_past_its_bulkhead_a_provider_refuses_at_once_without_a_mark_against_it() -> None:
    breaker = a_breaker(FakeClock(), threshold=1)
    guarded = GuardedProvider(ScriptedProvider(OK + OK), breaker, max_in_flight=1)
    holding = guarded.stream(REQUEST)
    await anext(holding)  # one call in flight

    with pytest.raises(GatewayBusyError):
        await collect(guarded.stream(REQUEST))
    await holding.aclose()

    assert breaker.state is BreakerState.CLOSED
    assert (await collect(guarded.stream(REQUEST))).content == "okok"  # its place is free


async def test_the_gateways_own_saturation_during_a_trial_frees_the_trial() -> None:
    class Busy:
        async def stream(self, request: ChatRequest) -> ChunkStream:
            raise GatewayBusyError("no free connection")
            yield  # pragma: no cover - makes this a generator

    clock = FakeClock()
    breaker = a_breaker(clock, threshold=1)
    breaker.record_failure()
    clock.now += 10  # half-open: the next call is the trial

    with pytest.raises(GatewayBusyError):
        await collect(GuardedProvider(Busy(), breaker).stream(REQUEST))

    assert breaker.state is BreakerState.HALF_OPEN  # nothing learned about the provider
    assert breaker.try_acquire() is Permit.TRIAL  # and the next call may try


class Pinging:
    """Keeps sending bytes that aren't an answer (as keep-alive pings would), never a chunk.
    Each read is prompt, so a per-read timeout never fires."""

    async def stream(self, request: ChatRequest) -> ChunkStream:
        while True:  # noqa: ASYNC110 - a ping every 10 ms, not waiting for an event
            await asyncio.sleep(0.01)
        yield CompletionChunk(content="never")  # pragma: no cover - makes this a generator


async def test_a_provider_that_never_starts_answering_fails_at_the_first_chunk_deadline() -> None:
    breaker = a_breaker(FakeClock(), threshold=1)
    guarded = GuardedProvider(Pinging(), breaker, first_chunk_timeout_s=0.1)

    started = time.monotonic()
    with pytest.raises(ProviderUnavailableError, match="no answer within"):
        await collect(guarded.stream(REQUEST))

    assert time.monotonic() - started < 1
    assert breaker.state is BreakerState.OPEN  # counted against it, like any outage


async def test_the_first_chunk_deadline_does_not_cut_a_long_answer_short() -> None:
    class SlowButSteady:
        async def stream(self, request: ChatRequest) -> ChunkStream:
            for word in ("a", "b", "c", "d"):
                await asyncio.sleep(0.05)
                yield CompletionChunk(content=word)

    breaker = a_breaker(FakeClock(), threshold=1)
    guarded = GuardedProvider(SlowButSteady(), breaker, first_chunk_timeout_s=0.1)

    completion = await collect(guarded.stream(REQUEST))  # 0.2 s in all, past the deadline

    assert completion.content == "abcd"
    assert breaker.state is BreakerState.CLOSED
