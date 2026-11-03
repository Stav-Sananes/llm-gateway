"""Routes a model name to an ordered list of provider targets, falling back down the list.

Fallback happens only before the first chunk (connect error, timeout, 5xx, 429, open
circuit). Once a provider has streamed, its output stands; resuming elsewhere mid-stream
is a separate build step.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from llm_gateway.chat.model import ChatRequest
from llm_gateway.chat.ports import ChatProvider, ChunkStream, ProviderUnavailableError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Target:
    provider: str
    model: str


class Router:
    def __init__(
        self,
        providers: Mapping[str, ChatProvider],
        routes: Mapping[str, Sequence[Target]],
        default_provider: str,
    ) -> None:
        named = {t.provider for targets in routes.values() for t in targets} | {default_provider}
        if missing := named - providers.keys():
            raise ValueError(f"routes name unknown providers: {sorted(missing)}")
        self._providers = providers
        self._routes = routes
        self._default_provider = default_provider

    def targets_for(self, model: str) -> Sequence[Target]:
        """A routed model name, or any other name sent as-is to the default provider."""
        return self._routes.get(model) or (Target(self._default_provider, model),)

    async def stream(self, request: ChatRequest) -> ChunkStream:
        failures: list[str] = []
        for target in self.targets_for(request.model):
            chunks = self._providers[target.provider].stream(replace(request, model=target.model))
            try:
                first = await anext(chunks)
            except StopAsyncIteration:
                return
            except ProviderUnavailableError as error:
                await chunks.aclose()
                logger.warning(
                    "%s/%s unavailable, trying next: %s", target.provider, target.model, error
                )
                failures.append(f"{target.provider}: {error}")
                continue
            try:
                yield first
                async for chunk in chunks:
                    yield chunk
            finally:
                await chunks.aclose()
            return
        raise ProviderUnavailableError("; ".join(failures))
