"""Admission: refuse new work while the process runs late or has too much in flight."""

import time

import anyio
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from llm_gateway.chat.ports import GatewayBusyError
from llm_gateway.http.admission import (
    Admission,
    AdmissionLimits,
    AtCapacityError,
    LoopLag,
    ShedWhileLagging,
)
from llm_gateway.http.errors import register_error_handlers

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


DEFAULT_LIMIT_S = AdmissionLimits().max_loop_lag_s


async def test_a_loop_that_keeps_running_late_shows_lag() -> None:
    assert await lag_after(0.3, 0.3, 0.3, 0.3, 0.3) > DEFAULT_LIMIT_S


async def test_one_ordinary_pause_alone_is_not_enough_to_shed() -> None:
    # A 120 ms pause (a long GC, a slow import) stays under the default limit.
    assert await lag_after(0.12) < DEFAULT_LIMIT_S


def test_the_door_refuses_only_while_the_loop_runs_late() -> None:
    shed = list[int]()
    late = an_admission(lag_s=0.25, shed=shed)
    full = an_admission(max_in_flight=1)
    full.admit()

    with pytest.raises(AtCapacityError):
        late.refuse_if_lagging()
    full.refuse_if_lagging()  # places are counted later, after the key is checked

    assert shed == [1]
    assert (late.in_flight, full.in_flight) == (0, 1)


class Recorder:
    """An ASGI app that records whether it ran and whether it read the body."""

    def __init__(self) -> None:
        self.called = False
        self.read_body = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.called = True
        await receive()
        self.read_body = True
        await JSONResponse({})(scope, receive, send)


async def through_the_door(path: str, lag_s: float) -> tuple[httpx.Response, Recorder]:
    app = Recorder()
    door = ShedWhileLagging(app, {"/v1/chat/completions": an_admission(lag_s=lag_s)})
    transport = httpx.ASGITransport(door)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post(path, content=b"{}"), app


async def test_while_the_loop_runs_late_the_door_refuses_without_reading_the_body() -> None:
    response, app = await through_the_door("/v1/chat/completions", lag_s=0.25)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "2"
    assert response.json()["error"]["type"] == "service_unavailable"
    assert not app.called


async def test_the_door_lets_requests_through_when_on_time_or_not_guarded() -> None:
    on_time, guarded_app = await through_the_door("/v1/chat/completions", lag_s=0.0)
    other, other_app = await through_the_door("/healthz", lag_s=0.25)

    assert (on_time.status_code, other.status_code) == (200, 200)
    assert guarded_app.read_body and other_app.read_body


def test_with_every_provider_busy_the_retry_hint_is_jittered_too() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/busy")
    async def busy() -> None:
        raise GatewayBusyError("every connection in use")

    with TestClient(app) as client:
        hints = {client.get("/busy").headers["retry-after"] for _ in range(40)}

    # Clients turned away together must not all come back in the same second.
    assert hints == {"1", "2", "3"}
