"""How `llm-gateway serve` starts uvicorn: the settings that shape shutdown and keep-alive."""

from typing import Any

import pytest

from llm_gateway import main
from llm_gateway.config import Settings


def test_serve_passes_the_shutdown_and_keep_alive_timeouts_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setattr(main, "build_app", lambda settings: "app")
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: served.update(app=app, **kwargs))

    main.run(Settings(shutdown_timeout_s=33, keep_alive_timeout_s=90))

    assert served["timeout_graceful_shutdown"] == 33  # held approvals close first (12)
    assert served["timeout_keep_alive"] == 90  # above the proxy's idle timeout


def test_the_default_keep_alive_outlasts_a_proxy_s_60_second_idle_timeout() -> None:
    assert Settings().keep_alive_timeout_s > 60
