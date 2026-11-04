"""Embedder over the OpenAI embeddings protocol (Ollama serves it at /v1/embeddings)."""

import math
from collections.abc import Sequence

import httpx

from llm_gateway.cache.ports import EmbedderUnavailableError


class OpenAICompatibleEmbedder:
    def __init__(self, client: httpx.AsyncClient, model: str, dimensions: int) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions

    async def embed(self, text: str) -> Sequence[float]:
        try:
            response = await self._client.post(
                "/embeddings", json={"model": self._model, "input": text}
            )
            response.raise_for_status()
            return _checked(response.json()["data"][0]["embedding"], self._dimensions)
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
            raise EmbedderUnavailableError(f"embedding failed: {type(error).__name__}") from error


def _checked(vector: object, dimensions: int) -> list[float]:
    if not isinstance(vector, list) or len(vector) != dimensions:
        got = len(vector) if isinstance(vector, list) else type(vector).__name__
        raise EmbedderUnavailableError(f"expected {dimensions} dimensions, got {got}")
    floats = [float(x) for x in vector]
    if not all(math.isfinite(x) for x in floats) or not any(floats):
        raise EmbedderUnavailableError("embedding has non-finite or all-zero values")
    return floats
