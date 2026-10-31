"""Minimal Server-Sent Events line parsing, shared by provider adapters."""

DONE_SENTINEL = "[DONE]"


def sse_data(line: str) -> str | None:
    """Return the payload of a `data:` line, or None for any other SSE line."""
    if not line.startswith("data:"):
        return None
    return line.removeprefix("data:").strip()
