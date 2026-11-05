"""The chaos tooling: the runner reading one sample out of /metrics, and the fake Claude's entry
point."""

from typing import Any

import pytest

from chaos.run import sample
from harness import fake_anthropic

EXPOSITION = """\
# HELP gateway_circuit_state 0 closed, 1 half-open, 2 open
gateway_circuit_state{provider="anthropic"} 2.0
gateway_circuit_state{provider="local"} 0.0
gateway_chat_requests_total{outcome="completed",provider="local"} 12.0
gateway_chat_requests_total{outcome="interrupted",provider="local"} 1.0
gateway_event_loop_lag_seconds 0.003
"""


def test_a_sample_is_found_by_its_exact_labels_in_any_order() -> None:
    assert sample(EXPOSITION, "gateway_circuit_state", provider="anthropic") == 2.0
    assert sample(
        EXPOSITION, "gateway_chat_requests_total", provider="local", outcome="completed"
    ) == 12.0  # fmt: skip


def test_a_sample_without_labels_is_found() -> None:
    assert sample(EXPOSITION, "gateway_event_loop_lag_seconds") == 0.003


def test_a_missing_sample_reads_zero_like_an_untouched_counter() -> None:
    assert sample(EXPOSITION, "gateway_chat_requests_total", provider="anthropic") == 0.0
    # A subset of the labels is not a match.
    assert sample(EXPOSITION, "gateway_chat_requests_total", provider="local") == 0.0


def test_the_fake_anthropic_serves_on_the_port_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setenv("FAKE_ANTHROPIC_PORT", "9124")
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.update(app=app, **kw))

    fake_anthropic.main()

    assert (served["host"], served["port"]) == ("0.0.0.0", 9124)  # noqa: S104 - in a container
