"""Routes a model name to an ordered list of provider targets, falling back down the list.

Before the first chunk (connect error, timeout, 5xx, 429, open circuit) the next target
simply gets the request. After it, a target that dies mid-answer is resumed: the next
target is asked to continue the partial answer (see chat/resume.py), and the client sees
one stream. A target that refuses the continuation (4xx) is skipped like a dead one; when
no target is left, the stream ends with ProviderUnavailableError, which the client gets
in-band. The first chunk each target serves carries its Origin, so the switch is
visible inside the gateway (usage rows, audit, the resume span) and nowhere else.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from opentelemetry import trace
from opentelemetry.trace import Tracer

from llm_gateway.chat.model import ChatRequest, CompletionChunk, Origin
from llm_gateway.chat.ports import (
    ChatProvider,
    ChunkStream,
    GatewayBusyError,
    ProviderRejectedError,
    ProviderUnavailableError,
)
from llm_gateway.chat.resume import Seam, continuation

logger = logging.getLogger(__name__)

_NOTHING = CompletionChunk()


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
        tracer: Tracer | None = None,
    ) -> None:
        named = {t.provider for targets in routes.values() for t in targets} | {default_provider}
        if missing := named - providers.keys():
            raise ValueError(f"routes name unknown providers: {sorted(missing)}")
        self._providers = providers
        self._routes = routes
        self._default_provider = default_provider
        self._tracer = tracer or trace.get_tracer(__name__)

    def targets_for(self, model: str) -> Sequence[Target]:
        """A routed model name, or any other name sent as-is to the default provider."""
        return self._routes.get(model) or (Target(self._default_provider, model),)

    async def stream(self, request: ChatRequest) -> ChunkStream:
        failures: list[str] = []
        all_busy = True  # every target refused because this gateway was saturated
        sent = ""  # the answer so far, as the client has seen it
        previous: Origin | None = None  # the target that died mid-answer
        for target in self.targets_for(request.model):
            ask = continuation(request, sent)
            if ask is None:  # the dead target had used up max_tokens: the answer is done
                yield CompletionChunk(finish_reason="length")
                return
            chunks = self._providers[target.provider].stream(replace(ask, model=target.model))
            try:
                first = await anext(chunks)
            except (StopAsyncIteration, ProviderUnavailableError) as error:
                await chunks.aclose()
                all_busy = all_busy and isinstance(error, GatewayBusyError)
                failures.append(self._unavailable(target, str(error) or "empty stream"))
                continue
            except ProviderRejectedError as error:
                await chunks.aclose()
                all_busy = False
                if previous is None:  # the client's own request: wrong everywhere
                    raise
                # Only the continuation was refused (say, the partial answer pushed it past
                # this model's context): another target may still take it.
                failures.append(self._unavailable(target, f"refused to continue: {error}"))
                continue
            origin = Origin(target.provider, target.model)
            if previous is not None:
                self._trace_resume(request, previous, origin, sent)
            seam, finished = Seam(sent), False
            chunk: CompletionChunk | None = replace(first, origin=origin)
            try:
                while chunk is not None:
                    out = seam.pass_on(chunk)
                    finished = finished or out.finish_reason is not None
                    sent += out.content
                    if out != _NOTHING:
                        yield out
                    chunk = await anext(chunks, None)
                if tail := seam.release():  # the stream ended without a finish chunk
                    sent += tail
                    yield CompletionChunk(content=tail)
                return
            except ProviderUnavailableError as error:
                if finished:  # the answer was whole; only its usage report was lost
                    raise
                failures.append(self._unavailable(target, f"died mid-answer: {error}"))
                previous, all_busy = origin, False
            finally:
                await chunks.aclose()
        if all_busy and failures:
            raise GatewayBusyError("; ".join(failures))  # 503 and Retry-After, not 502
        raise ProviderUnavailableError("; ".join(failures))

    def _unavailable(self, target: Target, reason: str) -> str:
        logger.warning("%s/%s unavailable, trying next: %s", target.provider, target.model, reason)
        return f"{target.provider}: {reason}"

    def _trace_resume(
        self, request: ChatRequest, dead: Origin, resumed: Origin, partial: str
    ) -> None:
        logger.warning(
            "resuming %s mid-answer: %s/%s died after %d chars, %s/%s continues",
            request.model, dead.provider, dead.model, len(partial), resumed.provider,
            resumed.model,
        )  # fmt: skip
        self._tracer.start_span(
            "gateway.resume",
            attributes={
                "gen_ai.request.model": request.model,
                "gateway.resume.from.provider": dead.provider,
                "gateway.resume.from.model": dead.model,
                "gateway.resume.to.provider": resumed.provider,
                "gateway.resume.to.model": resumed.model,
                "gateway.resume.partial_chars": len(partial),
            },
        ).end()
