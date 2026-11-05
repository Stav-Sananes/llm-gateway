"""The chaos tooling: the runner reading one sample out of /metrics, and the fake Claude's entry
point."""

from typing import Any

import pytest

from chaos.run import (
    UNKNOWN_KEY,
    Answer,
    MissingSampleError,
    Tenants,
    _redis_verdict,
    sample,
    scenarios,
    slug,
)
from harness import fake_anthropic
from llm_gateway.tenancy.keys import looks_like_key

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
    assert "/v1/messages" in {route.path for route in served["app"].routes}  # the fake Claude


def test_a_missing_gauge_is_an_error_when_required() -> None:
    # A circuit gauge that's gone must not read as 0, "closed".
    with pytest.raises(MissingSampleError):
        sample(EXPOSITION, "gateway_circuit_state", required=True, provider="nope")


def test_the_unknown_key_is_well_formed_so_it_is_really_looked_up() -> None:
    # Malformed, it would be refused on its shape alone, and prove nothing about Postgres.
    assert looks_like_key(UNKNOWN_KEY)


BUDGET_DOWN = "Usage limits are temporarily unavailable."


@pytest.mark.parametrize(
    ("answers", "unknown", "held"),
    [
        ([Answer(503, 0.3, BUDGET_DOWN)] * 5, Answer(401, 0.3), True),
        (
            [Answer(503, 0.3, "Authentication is temporarily unavailable.")] * 5,
            Answer(401, 0.3),
            False,
        ),
        ([Answer(503, 0.3, BUDGET_DOWN)] * 5, Answer(503, 0.3), False),
        ([Answer(503, 1.2, BUDGET_DOWN)] * 5, Answer(401, 0.3), False),
        ([Answer(200, 0.1)] * 5, Answer(401, 0.3), False),
    ],
    ids=["budget-fails-closed", "wrong-503", "unknown-key-not-refused", "too-slow", "fails-open"],
)
def test_the_redis_verdict_rejects_each_way_the_gateway_could_get_it_wrong(
    answers: list[Answer], unknown: Answer, held: bool
) -> None:
    assert _redis_verdict(answers, unknown).held is held


def test_scenario_names_are_known_without_touching_the_stack() -> None:
    # Tenants are created lazily, so listing the scenarios runs no docker command.
    names = [slug(scenario.name) for scenario in scenarios(Tenants())]

    assert "postgres-down" in names and "provider-slow-under-load" in names
    assert len(names) == len(set(names))
