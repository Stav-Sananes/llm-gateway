"""Runs ASGI apps on real sockets, so tests exercise real HTTP streaming."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI

STARTUP_TIMEOUT_S = 5.0


@contextmanager
def serving(app: FastAPI) -> Iterator[str]:
    """Serve `app` on an ephemeral localhost port; yield its base URL."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        _wait_until_started(server)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=STARTUP_TIMEOUT_S)
        sock.close()


def unused_local_url() -> str:
    """A URL nothing listens on: the port is bound, read, then released."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _wait_until_started(server: uvicorn.Server) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while not server.started:
        if time.monotonic() > deadline:
            raise TimeoutError("server did not start")
        time.sleep(0.01)
