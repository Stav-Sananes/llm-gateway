"""A throwaway Redis for tests."""

from collections.abc import Iterator
from contextlib import contextmanager

from testcontainers.community.redis import RedisContainer

REDIS_IMAGE = "redis:8.10.3-alpine"


@contextmanager
def running_redis() -> Iterator[str]:
    """Start Redis in Docker and yield its URL."""
    with RedisContainer(REDIS_IMAGE) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(container.port)
        yield f"redis://{host}:{port}/0"
