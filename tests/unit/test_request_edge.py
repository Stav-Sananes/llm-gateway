"""What the edge refuses before a request reaches the use case: bad model names, absurd
max_tokens, oversized bodies."""

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import ValidationError

from llm_gateway.http.body_limit import BodyLimit
from llm_gateway.http.schemas import MAX_COMPLETION_TOKENS, ChatCompletionBody

pytestmark = pytest.mark.anyio

MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    "model", ["fake\x00", "tab\there", "\ud800", "x" * 257], ids=["nul", "tab", "surrogate", "long"]
)
def test_a_model_name_that_could_poison_logs_or_storage_is_refused(model: str) -> None:
    with pytest.raises(ValidationError):
        ChatCompletionBody(model=model, messages=MESSAGES)


def test_an_ordinary_model_name_is_accepted() -> None:
    assert ChatCompletionBody(model="qwen2.5:0.5b", messages=MESSAGES).model == "qwen2.5:0.5b"


def test_max_tokens_has_an_upper_bound() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionBody(model="m", messages=MESSAGES, max_tokens=MAX_COMPLETION_TOKENS + 1)


def echo_app() -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    return app


async def post(body: bytes | list[bytes], max_bytes: int = 100) -> httpx.Response:
    transport = httpx.ASGITransport(BodyLimit(echo_app(), max_bytes=max_bytes))
    content = body if isinstance(body, bytes) else iter_chunks(body)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post("/echo", content=content)


async def iter_chunks(chunks: list[bytes]):  # type: ignore[no-untyped-def]
    for chunk in chunks:
        yield chunk


async def test_a_body_within_the_limit_passes() -> None:
    response = await post(b"x" * 100)

    assert response.json() == {"bytes": 100}


async def test_a_declared_body_over_the_limit_is_a_413_without_being_read() -> None:
    response = await post(b"x" * 101)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


async def test_a_chunked_body_is_cut_off_once_it_goes_over() -> None:
    response = await post([b"x" * 60, b"x" * 60])  # no Content-Length

    assert response.status_code == 413
