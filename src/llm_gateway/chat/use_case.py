"""The chat completion use case.

Thin today: one provider, no policy. Auth, rate limit, budget, cache and routing
land here in later build steps, which is why the HTTP layer talks to this class
and not to a provider directly.
"""

from llm_gateway.chat.model import ChatRequest
from llm_gateway.chat.ports import ChatProvider, ChunkStream


class ChatCompletions:
    def __init__(self, provider: ChatProvider) -> None:
        self._provider = provider

    def stream(self, request: ChatRequest) -> ChunkStream:
        return self._provider.stream(request)
