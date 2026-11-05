import pytest
from pydantic import ValidationError

from llm_gateway.config import Settings


@pytest.mark.parametrize(
    "field",
    ["rate_limit_burst", "rate_limit_per_s", "token_budget_per_day", "default_max_tokens"],
)
def test_limits_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: 0})


def test_budgets_can_be_switched_off() -> None:
    assert Settings(token_budget_per_day=None).token_budget_per_day is None


def test_a_blank_anthropic_key_means_anthropic_is_off() -> None:
    assert Settings.model_validate({"anthropic_api_key": ""}).anthropic_api_key is None


def test_approvals_must_poll_more_often_than_they_time_out() -> None:
    with pytest.raises(ValueError, match="approval_poll_s"):
        Settings(approval_timeout_s=1, approval_poll_s=1)


def test_held_calls_wait_less_than_mcp_clients_time_out_by_default() -> None:
    settings = Settings()

    assert settings.approval_timeout_s < 60  # the TypeScript MCP SDK's request timeout
    assert (settings.approval_poll_s, settings.approval_max_pending_per_tenant) == (0.5, 10)
    with pytest.raises(ValueError, match="3600"):
        Settings(approval_timeout_s=3601)


@pytest.mark.parametrize(
    "field", ["max_concurrent_chats", "max_concurrent_tool_calls", "max_loop_lag_s"]
)
def test_admission_limits_must_be_positive(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        Settings.model_validate({field: 0})
