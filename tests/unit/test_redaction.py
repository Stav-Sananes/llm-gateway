import pytest

from llm_gateway.audit.chain import UnauditableEventError
from llm_gateway.audit.redaction import REDACTED, redact
from llm_gateway.tenancy.keys import issue_key


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "DB_PASSWORD",
        "api_key",
        "x-api-key",
        "Authorization",
        "token",
        "access_token",
        "sessionToken",
        "client_secret",
        "Cookie",
        "apikey",
    ],
)
def test_values_under_secret_keys_are_redacted(key: str) -> None:
    assert redact({key: "hunter2"}) == {key: REDACTED}


def test_credentials_are_redacted_wherever_they_appear() -> None:
    gateway_key = issue_key().plaintext
    query = f"SELECT 1 -- {gateway_key} and sk-ant-api03-abcdef"

    redacted = redact({"arguments": {"sql": query}})

    assert gateway_key not in str(redacted)
    assert "sk-ant-api03" not in str(redacted)
    assert redacted["arguments"]["sql"].startswith("SELECT 1 -- ")


def test_nested_lists_tuples_and_dicts_are_walked() -> None:
    assert redact({"a": [{"secret": "x"}, "plain"]}) == {"a": [{"secret": REDACTED}, "plain"]}
    assert redact({"t": ({"token": "x"},)}) == {"t": [{"token": REDACTED}]}


def test_ordinary_values_are_left_alone() -> None:
    details = {"tool": "fs__write", "arguments": {"path": "/sandbox/a.txt", "n": 3}}

    assert redact(details) == details


def test_callers_can_name_more_secret_keys() -> None:
    assert redact({"ssn": "123"}, extra_keys=["ssn"]) == {"ssn": REDACTED}


@pytest.mark.parametrize("key", ["max_tokens", "input_tokens", "token_count", "path", "sql"])
def test_ordinary_keys_that_mention_tokens_are_kept(key: str) -> None:
    assert redact({key: 42}) == {key: 42}


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-" + "a" * 40,
        "Bearer abc.def-ghi",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
        "sk-" + "A1b2" * 6,
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    ],
    ids=["openai-project-key", "bearer", "jwt", "openai-key", "pem"],
)
def test_more_credential_shapes_are_redacted_in_values(secret: str) -> None:
    assert redact({"note": f"used {secret} here"})["note"] == f"used {REDACTED} here"


def test_a_non_string_key_is_refused_rather_than_merged() -> None:
    with pytest.raises(UnauditableEventError):
        redact({1: "a", "1": "b"})
