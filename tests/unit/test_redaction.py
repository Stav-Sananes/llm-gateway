import pytest

from llm_gateway.audit.redaction import REDACTED, redact
from llm_gateway.tenancy.keys import issue_key


@pytest.mark.parametrize("key", ["password", "DB_PASSWORD", "api_key", "Authorization", "token"])
def test_values_under_secret_keys_are_redacted(key: str) -> None:
    assert redact({key: "hunter2"}) == {key: REDACTED}


def test_credentials_are_redacted_wherever_they_appear() -> None:
    gateway_key = issue_key().plaintext
    query = f"SELECT 1 -- {gateway_key} and sk-ant-api03-abcdef"

    redacted = redact({"arguments": {"sql": query}})

    assert gateway_key not in str(redacted)
    assert "sk-ant-api03" not in str(redacted)
    assert redacted["arguments"]["sql"].startswith("SELECT 1 -- ")


def test_nested_lists_and_dicts_are_walked() -> None:
    assert redact({"a": [{"secret": "x"}, "plain"]}) == {"a": [{"secret": REDACTED}, "plain"]}


def test_ordinary_values_are_left_alone() -> None:
    details = {"tool": "fs__write", "arguments": {"path": "/sandbox/a.txt", "n": 3}}

    assert redact(details) == details


def test_callers_can_name_more_secret_keys() -> None:
    assert redact({"ssn": "123"}, extra_keys=["ssn"]) == {"ssn": REDACTED}
