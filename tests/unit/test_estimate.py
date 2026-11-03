from llm_gateway.limits.estimate import estimate_prompt_tokens


def test_estimate_is_utf8_bytes_of_the_messages_over_three_rounded_up() -> None:
    # json.dumps of this list is 45 bytes.
    assert estimate_prompt_tokens([{"role": "user", "content": "hello there"}]) == 15


def test_longer_prompts_estimate_more_tokens() -> None:
    short = [{"role": "user", "content": "hi"}]
    long = [{"role": "user", "content": "hi " * 400}]

    assert estimate_prompt_tokens(long) > estimate_prompt_tokens(short)


def test_english_is_over_rather_than_under_estimated() -> None:
    text = "the quick brown fox jumps over the lazy dog " * 100  # ~1,000 real tokens

    assert estimate_prompt_tokens([{"role": "user", "content": text}]) >= 1000


def test_chinese_text_is_not_under_estimated() -> None:
    text = "你好世界" * 250  # about one token per character

    assert estimate_prompt_tokens([{"role": "user", "content": text}]) >= 1000


def test_fields_other_than_content_count_too() -> None:
    plain = {"role": "assistant", "content": ""}
    with_tool_call = plain | {"tool_calls": [{"function": {"arguments": "x" * 3000}}]}

    assert estimate_prompt_tokens([with_tool_call]) >= estimate_prompt_tokens([plain]) + 1000
