"""What the chat use case needs from the outside world."""

from collections.abc import AsyncGenerator
from typing import Protocol
from uuid import UUID

from llm_gateway.chat.model import ChatRequest, CompletionChunk, Origin

# An AsyncGenerator (not just an iterator) so callers can aclose() it when the
# client disconnects mid-stream, releasing the upstream connection immediately.
ChunkStream = AsyncGenerator[CompletionChunk, None]


class ProviderUnavailableError(Exception):
    """The provider could not serve the request: connect error, timeout, 5xx, broken stream.

    Retryable on another provider. Fallback (build step 4) keys off this type.
    """


class ProviderRejectedError(Exception):
    """The provider refused the request itself (4xx). Not retryable elsewhere."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class ChatProvider(Protocol):
    def stream(self, request: ChatRequest) -> ChunkStream:
        """Stream the completion. Raises ProviderUnavailableError / ProviderRejectedError."""
        ...


class ProviderSwitches(Protocol):
    """Where the gateway notes that one provider continued an answer another one began."""

    async def record(
        self, tenant_id: UUID, requested_model: str, dead: Origin, resumed: Origin
    ) -> None:
        """Never raises: a lost note must not fail a request that was answered."""
        ...


class NoSwitchRecording:
    async def record(
        self, tenant_id: UUID, requested_model: str, dead: Origin, resumed: Origin
    ) -> None:
        pass
