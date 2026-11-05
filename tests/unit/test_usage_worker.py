import anyio
import pytest

from llm_gateway.usage.ports import Delivery, UsageStoreUnavailableError
from llm_gateway.usage.worker import Drained, UsageWorker
from tests.support.usage import InMemoryUsageQueue, InMemoryUsageStore, a_usage_event

pytestmark = pytest.mark.anyio


def delivered(n: int, **changes: object) -> Delivery:
    return Delivery(f"{n}-0", a_usage_event(**changes), raw="{...}")


class Observer:
    def __init__(self) -> None:
        self.drained_results: list[Drained] = []
        self.failures: list[Exception] = []
        self.failed_twice, self.wrote = anyio.Event(), anyio.Event()

    def drained(self, result: Drained) -> None:
        self.drained_results.append(result)
        if result.written:
            self.wrote.set()

    def failed(self, error: Exception) -> None:
        self.failures.append(error)
        if len(self.failures) >= 2:
            self.failed_twice.set()


async def test_a_batch_is_stored_then_acknowledged() -> None:
    queue, store = InMemoryUsageQueue(delivered(1), delivered(2)), InMemoryUsageStore()

    result = await UsageWorker(queue, store).drain_once()

    assert result == Drained(written=2)
    assert len(store.events) == 2
    assert queue.acked == ["1-0", "2-0"]


async def test_with_the_store_down_nothing_is_acknowledged_so_it_is_delivered_again() -> None:
    queue = InMemoryUsageQueue(delivered(1))

    with pytest.raises(UsageStoreUnavailableError):
        await UsageWorker(queue, InMemoryUsageStore(down=True)).drain_once()
    store = InMemoryUsageStore()
    await UsageWorker(queue, store).drain_once()

    assert len(store.events) == 1
    assert queue.acked == ["1-0"]


async def test_an_undecodable_delivery_is_dead_lettered_and_the_rest_stored() -> None:
    garbage = Delivery("1-0", None, raw="not json")
    queue, store = InMemoryUsageQueue(garbage, delivered(2)), InMemoryUsageStore()

    result = await UsageWorker(queue, store).drain_once()

    assert result == Drained(written=1, dead_lettered=1)
    assert queue.dead == [garbage]
    assert sorted(queue.acked) == ["1-0", "2-0"]


async def test_an_event_the_store_refuses_does_not_block_the_others() -> None:
    bad = delivered(2, prompt_tokens=-1)
    queue, store = InMemoryUsageQueue(delivered(1), bad, delivered(3)), InMemoryUsageStore()

    result = await UsageWorker(queue, store).drain_once()

    assert result == Drained(written=2, dead_lettered=1)
    assert queue.dead == [bad]
    assert len(store.events) == 2
    assert sorted(queue.acked) == ["1-0", "2-0", "3-0"]


async def test_a_redelivered_event_is_stored_once() -> None:
    again = delivered(1)
    store = InMemoryUsageStore()

    await UsageWorker(InMemoryUsageQueue(again), store).drain_once()
    await UsageWorker(InMemoryUsageQueue(Delivery("9-0", again.event, "")), store).drain_once()

    assert len(store.events) == 1


async def test_run_waits_out_an_outage_and_carries_on() -> None:
    queue, store = InMemoryUsageQueue(delivered(1), down=True), InMemoryUsageStore()
    observer = Observer()
    worker = UsageWorker(queue, store, observer=observer)

    with anyio.fail_after(2):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(worker.run, 0.01, 0.02)
            await observer.failed_twice.wait()
            queue.down = False
            await observer.wrote.wait()
            tasks.cancel_scope.cancel()

    assert len(store.events) == 1
    assert observer.drained_results[0] == Drained(written=1)


async def test_run_waits_out_a_store_outage_and_carries_on() -> None:
    # Postgres down: the worker must wait, not crash (a restart would hide it in production).
    queue, store = InMemoryUsageQueue(delivered(1)), InMemoryUsageStore(down=True)
    observer = Observer()
    worker = UsageWorker(queue, store, observer=observer)

    with anyio.fail_after(2):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(worker.run, 0.01, 0.02)
            await observer.failed_twice.wait()
            store.down = False
            await observer.wrote.wait()
            tasks.cancel_scope.cancel()

    assert len(store.events) == 1
    assert queue.acked == ["1-0"]  # acknowledged only once stored


async def test_an_event_that_fails_while_the_store_is_up_is_isolated_not_retried_forever() -> None:
    # The store answers, but this batch fails with an error that looks like an outage.
    poison = delivered(2, requested_model="m\x00")
    queue, store = InMemoryUsageQueue(delivered(1), poison, delivered(3)), InMemoryUsageStore()

    result = await UsageWorker(queue, store).drain_once()

    assert result == Drained(written=2, dead_lettered=1)
    assert queue.dead == [poison]


async def test_an_entry_trimmed_before_it_was_stored_is_counted_lost_not_dead_lettered() -> None:
    trimmed = Delivery("1-0", None, raw="")
    queue = InMemoryUsageQueue(trimmed, delivered(2))

    result = await UsageWorker(queue, InMemoryUsageStore()).drain_once()

    assert result == Drained(written=1, lost=1)
    assert queue.dead == []
    assert sorted(queue.acked) == ["1-0", "2-0"]


async def test_run_backs_off_doubling_up_to_the_cap_and_resets_after_a_drain() -> None:
    queue, store = InMemoryUsageQueue(delivered(1), down=True), InMemoryUsageStore()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 6:
            queue.down = False  # Redis comes back
        if len(sleeps) == 8:
            raise anyio.get_cancelled_exc_class()
        await anyio.lowlevel.checkpoint()

    with pytest.raises(anyio.get_cancelled_exc_class()):
        await UsageWorker(queue, store, sleep=sleep).run(retry_s=1, max_retry_s=8, idle_s=0.5)

    # Six failures back off 1, 2, 4, 8, 8, 8; then a drain, then idle sleeps only.
    assert sleeps == [1, 2, 4, 8, 8, 8, 0.5, 0.5]
    assert len(store.events) == 1
