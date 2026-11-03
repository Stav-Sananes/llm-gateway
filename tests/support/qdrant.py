"""A throwaway Qdrant for tests."""

from collections.abc import Iterator
from contextlib import contextmanager

from testcontainers.community.qdrant import QdrantContainer

QDRANT_IMAGE = "qdrant/qdrant:v1.20.0"


@contextmanager
def running_qdrant() -> Iterator[str]:
    """Start Qdrant in Docker and yield its REST URL."""
    with QdrantContainer(QDRANT_IMAGE) as container:
        yield f"http://{container.rest_host_address}"
