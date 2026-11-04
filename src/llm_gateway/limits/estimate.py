"""A cheap upper-bound guess of a prompt's token count, made before the provider counts it."""

import json
from collections.abc import Sequence

from llm_gateway.chat.model import Message

# Whole messages are forwarded upstream (content, names, tool calls), so all of it counts.
# UTF-8 bytes / 3 over-counts English (about 4 characters per token) and still covers
# scripts like Chinese, where one 3-byte character is often one token.
_BYTES_PER_TOKEN = 3


def estimate_prompt_tokens(messages: Sequence[Message]) -> int:
    size = len(json.dumps(list(messages), ensure_ascii=False, default=str).encode())
    return -(-size // _BYTES_PER_TOKEN)


def estimate_text_tokens(text: str) -> int:
    """The same upper-bound guess for plain text, e.g. an answer streamed so far."""
    return -(-len(text.encode()) // _BYTES_PER_TOKEN)
