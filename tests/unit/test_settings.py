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
