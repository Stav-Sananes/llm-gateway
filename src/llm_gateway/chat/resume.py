"""Mid-stream resume: when a provider dies after it has started answering, another one
continues the same answer, and the client sees one unbroken stream.

Current Claude models refuse assistant prefill, so the continuation is asked for in
words: the partial answer goes back as an assistant turn, followed by a user turn asking
for the rest. Models sometimes repeat the last words anyway (or start over); the Seam
drops that repeat before the client sees it.
"""

from dataclasses import replace

from llm_gateway.chat.model import ChatRequest, CompletionChunk
from llm_gateway.limits.estimate import estimate_text_tokens

CONTINUE_INSTRUCTION = (
    "Your previous reply was cut off. Continue it from exactly where it stopped, "
    "mid-word or mid-sentence if that is where it ended. Do not repeat anything already "
    "written, and do not mention the interruption."
)
MIN_OVERLAP_CHARS = 8  # shorter repeats are too likely to be real text ("the", "a ")
MAX_OVERLAP_CHARS = 2000  # how far back a repeat is looked for, and held at most


def continuation(request: ChatRequest, partial: str) -> ChatRequest | None:
    """The request that asks for the rest of `partial`. None when the answer has already
    used its whole max_tokens, so there is nothing left to ask for."""
    if not partial.strip():
        # Nothing was said yet (or only whitespace, which some APIs refuse as an assistant
        # turn): just ask again.
        return request
    max_tokens = request.max_tokens
    if max_tokens is not None:
        max_tokens -= estimate_text_tokens(partial)  # an over-estimate: never past the cap
        if max_tokens < 1:
            return None
    messages = (
        *request.messages,
        {"role": "assistant", "content": partial},
        {"role": "user", "content": CONTINUE_INSTRUCTION},
    )
    return replace(request, messages=messages, max_tokens=max_tokens)


class Seam:
    """Where a continuation joins the partial answer. Drops the continuation's opening
    when it repeats the end of the partial answer (at least MIN_OVERLAP_CHARS of it).

    While the continuation so far could still be such a repeat, its text is held back;
    as soon as it can't, the text is released, minus the longest repeat found."""

    def __init__(self, partial: str) -> None:
        self._tail = partial[-MAX_OVERLAP_CHARS:]
        self._held = ""
        self._joined = len(self._tail) < MIN_OVERLAP_CHARS  # too short to repeat
        self.dropped = 0  # characters of repeat removed

    def pass_on(self, chunk: CompletionChunk) -> CompletionChunk:
        """The chunk as the client should see it; its text may be held back for now."""
        if self._joined:
            return chunk
        self._held += chunk.content
        ends = chunk.finish_reason is not None or chunk.usage is not None
        content = self.release() if ends or not self._may_be_a_repeat() else ""
        return replace(chunk, content=content)

    def release(self) -> str:
        """Join now: the text held back, minus the repeat. Call when the stream ends."""
        if self._joined:
            return ""
        self._joined = True
        held, self._held = self._held, ""
        self.dropped = self._repeat_length(held)
        return held[self.dropped :]

    def _may_be_a_repeat(self) -> bool:
        # Some suffix of the tail longer than the held text still starts with it.
        return self._tail.find(self._held, 0, len(self._tail) - 1) != -1

    def _repeat_length(self, held: str) -> int:
        for length in range(min(len(self._tail), len(held)), MIN_OVERLAP_CHARS - 1, -1):
            if self._tail.endswith(held[:length]):
                return length
        return 0
