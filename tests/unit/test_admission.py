"""Admission: refuse new work while the process runs late or has too much in flight."""

import time

import anyio
import pytest

from llm_gateway.http.admission import Admission, AdmissionLimits, AtCapacityError, LoopLag

pytestmark = pytest.mark.anyio


def an_admission(
    max_in_flight: int = 2, lag_s: float = 0.0, shed: list[int] | None = None
) -> Admission:
    lag = LoopLag()
    lag.lag_s = lag_s
    shed_log = shed if shed is not None else []
    return Admission(
        lag, AdmissionLimits(max_in_flight, max_loop_lag_s=0.1), lambda: shed_log.append(1),
        jitter=lambda: 2,
    )  # fmt: skip


def test_past_the_cap_new_work_is_refused_and_counted() -> None:
    shed = list[int]()
    admission = an_admission(max_in_flight=2, shed=shed)
    admission.admit(), admission.admit()

    with pytest.raises(AtCapacityError) as refused:
        admission.admit()

    assert refused.value.retry_after_s == 2
    assert shed == [1]


def test_a_released_ticket_frees_its_place_once() -> None:
    admission = an_admission(max_in_flight=1)
    ticket = admission.admit()

    ticket.release()
    ticket.release()  # a second release frees nothing more

    assert admission.in_flight == 0
    admission.admit()
    with pytest.raises(AtCapacityError):
        admission.admit()


def test_while_the_loop_runs_late_everything_is_refused() -> None:
    admission = an_admission(max_in_flight=100, lag_s=0.25)

    with pytest.raises(AtCapacityError):
        admission.admit()
    assert admission.in_flight == 0


async def lag_after(*blocks_s: float) -> float:
    """The lag once the loop has been blocked for each of `blocks_s` in turn."""
    lag = LoopLag()
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(lag.run)
        await anyio.sleep(0.12)
        assert lag.lag_s < 0.05  # an idle loop is on time
        for block_s in blocks_s:
            time.sleep(block_s)  # noqa: ASYNC251 - blocking the loop on purpose
            await anyio.sleep(0.06)  # one sample sees it
        tasks.cancel_scope.cancel()
    return lag.lag_s


async def test_a_loop_that_keeps_running_late_shows_lag() -> None:
    assert await lag_after(0.3, 0.3, 0.3, 0.3, 0.3) > 0.1  # past the default limit


async def test_one_blip_alone_is_not_enough_to_shed() -> None:
    assert await lag_after(0.2) < 0.1
