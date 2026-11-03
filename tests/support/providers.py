"""A scripted chat provider for unit tests."""

from collections.abc import Sequence

from llm_gateway.chat.model import ChatRequest, CompletionChunk
from llm_gateway.chat.ports import ChunkStream, ProviderRejectedError, ProviderUnavailableError


class ScriptedProvider:
    """Streams the given chunks; optionally fails after them (a dropped upstream) or rejects."""

    def __init__(
        self, chunks: Sequence[CompletionChunk], fail_after: bool = False, rejects: bool = False
    ) -> None:
        self._chunks = chunks
        self._fail_after = fail_after
        self._rejects = rejects
        self.requests: list[ChatRequest] = []
        self.closed = False  # the upstream connection was released

    async def stream(self, request: ChatRequest) -> ChunkStream:
        self.requests.append(request)
        try:
            if self._rejects:
                raise ProviderRejectedError(400, "bad request")
            for chunk in self._chunks:
                yield chunk
            if self._fail_after:
                raise ProviderUnavailableError("upstream dropped the stream")
        finally:
            self.closed = True
