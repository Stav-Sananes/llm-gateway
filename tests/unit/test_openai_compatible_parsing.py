from llm_gateway.chat.model import ChatRequest, CompletionChunk, Usage
from llm_gateway.providers.openai_compatible import parse_chunk, to_wire_request
from llm_gateway.providers.sse import sse_data


def test_sse_data_extracts_payload_and_ignores_other_lines() -> None:
    assert sse_data('data: {"a":1}') == '{"a":1}'
    assert sse_data("data:[DONE]") == "[DONE]"
    assert sse_data("event: ping") is None
    assert sse_data(": keep-alive comment") is None
    assert sse_data("") is None


def test_parse_chunk_reads_content_delta() -> None:
    payload = {"choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]}

    assert parse_chunk(payload) == CompletionChunk(content="Hi")


def test_parse_chunk_reads_finish_reason_without_content() -> None:
    payload = {"choices": [{"delta": {}, "finish_reason": "length"}]}

    assert parse_chunk(payload) == CompletionChunk(finish_reason="length")


def test_parse_chunk_reads_usage_only_chunk() -> None:
    payload = {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}

    assert parse_chunk(payload) == CompletionChunk(usage=Usage(3, 4))


def test_wire_request_omits_unset_options() -> None:
    request = ChatRequest(model="m", messages=({"role": "user", "content": "x"},))

    assert to_wire_request(request) == {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
