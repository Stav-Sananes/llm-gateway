"""A cheap upper-bound guess of a prompt's token count, made before the provider counts it."""

from collections.abc import Sequence

from llm_gateway.chat.model import Message

# English averages about 4 characters per token; per-message overhead covers role markers.
_CHARS_PER_TOKEN = 4
_TOKENS_PER_MESSAGE = 4


def estimate_prompt_tokens(messages: Sequence[Message]) -> int:
    characters = sum(len(str(message.get("content", ""))) for message in messages)
    return -(-characters // _CHARS_PER_TOKEN) + _TOKENS_PER_MESSAGE * len(messages)
