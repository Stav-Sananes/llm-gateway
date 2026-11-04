"""Asking another model to continue an answer, and joining the two without a repeat."""

from llm_gateway.chat.model import ChatRequest, CompletionChunk, Usage
from llm_gateway.chat.resume import CONTINUE_INSTRUCTION, Seam, continuation

QUESTION = ChatRequest("m", ({"role": "user", "content": "What is the capital of France?"},))
PARTIAL = "The capital of France"


def joined(partial: str, *continued: str) -> str:
    """What the client sees of the continuation, chunk by chunk, then at its end."""
    seam = Seam(partial)
    seen = [seam.pass_on(CompletionChunk(content=text)).content for text in continued]
    return "".join(seen) + seam.release()


def test_the_partial_answer_goes_back_followed_by_a_request_to_continue() -> None:
    asked = continuation(QUESTION, PARTIAL)

    assert asked is not None
    assert asked.messages == (
        *QUESTION.messages,
        {"role": "assistant", "content": PARTIAL},
        {"role": "user", "content": CONTINUE_INSTRUCTION},
    )


def test_with_nothing_said_yet_the_request_is_asked_again_unchanged() -> None:
    assert continuation(QUESTION, "") == QUESTION


def test_an_answer_that_used_its_whole_max_tokens_has_nothing_left_to_ask_for() -> None:
    capped = ChatRequest("m", QUESTION.messages, max_tokens=10)

    assert continuation(capped, "x" * 30) is None


def test_a_continuation_that_starts_fresh_passes_straight_through() -> None:
    seam = Seam(PARTIAL)

    assert seam.pass_on(CompletionChunk(content=" is Paris.")).content == " is Paris."


def test_a_repeat_of_the_last_words_is_dropped_even_across_chunks() -> None:
    assert joined(PARTIAL, "of Fr", "ance is", " Paris.") == " is Paris."


def test_a_continuation_that_starts_the_answer_over_is_joined_where_it_left_off() -> None:
    assert joined(PARTIAL, "The capital of France is Paris.") == " is Paris."


def test_text_that_may_be_a_repeat_is_held_until_it_cant_be() -> None:
    seam = Seam(PARTIAL)

    assert seam.pass_on(CompletionChunk(content="of Fran")).content == ""
    assert seam.pass_on(CompletionChunk(content="kly, no.")).content == "of Frankly, no."


def test_a_short_repeat_is_kept_as_real_text() -> None:
    # "France" (6 characters) could be a repeat or a new word: too short to drop.
    assert joined(PARTIAL, "France, a country.") == "France, a country."


def test_text_held_back_is_released_with_the_finish_chunk() -> None:
    seam = Seam(PARTIAL)
    seam.pass_on(CompletionChunk(content="of Fra"))

    end = seam.pass_on(CompletionChunk(finish_reason="stop", usage=Usage(1, 2)))

    assert end == CompletionChunk(content="of Fra", finish_reason="stop", usage=Usage(1, 2))


def test_nothing_is_held_after_a_partial_too_short_to_repeat() -> None:
    assert Seam("The").pass_on(CompletionChunk(content="The")).content == "The"


def test_a_repeat_of_exactly_8_characters_is_dropped() -> None:
    assert joined("I think France", "k France is big.") == " is big."  # "k France": 8


def test_a_repeat_of_7_characters_is_kept() -> None:
    assert joined("I think France", " France is big.") == " France is big."  # 7


def test_a_restart_is_found_up_to_2000_characters_back() -> None:
    partial = " ".join(f"word{i}" for i in range(300))[:1990]

    assert joined(partial, partial + " and more.") == " and more."


def test_a_restart_further_back_than_2000_characters_is_not_found() -> None:
    partial = " ".join(f"word{i}" for i in range(400))[:2100]

    assert joined(partial, partial + " and more.") == partial + " and more."
