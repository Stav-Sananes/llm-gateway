"""The OpenAI-compatible adapter against a real (fake) HTTP upstream."""

import anyio
import httpx
import pytest

from harness.fake_llm import FakeLlmScript, create_fake_llm
from llm_gateway.chat.model import ChatRequest, collect
from llm_gateway.chat.ports import GatewayBusyError, ProviderUnavailableError
from llm_gateway.providers.openai_compatible import OpenAICompatibleProvider
from tests.support.servers import serving

pytestmark = pytest.mark.anyio

QUESTION = ChatRequest("m", ({"role": "user", "content": "hi"},))


async def test_no_free_connection_is_the_gateway_busy_not_the_provider_down() -> None:
    slow = FakeLlmScript(token_delay_s=0.2)  # holds its one connection for a second
    with serving(create_fake_llm(slow)) as url:
        async with httpx.AsyncClient(
            base_url=f"{url}/v1",
            timeout=httpx.Timeout(5, pool=0.05),
            limits=httpx.Limits(max_connections=1),
        ) as client:
            provider = OpenAICompatibleProvider(client, "local")
            errors: list[Exception] = []

            async def ask() -> None:
                try:
                    await collect(provider.stream(QUESTION))
                except ProviderUnavailableError as error:
                    errors.append(error)

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(ask)
                await anyio.sleep(0.1)  # the first call holds the only connection
                tasks.start_soon(ask)

    (error,) = errors
    assert isinstance(error, GatewayBusyError)
