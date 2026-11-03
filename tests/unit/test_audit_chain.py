import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from llm_gateway.audit.chain import (
    EMPTY_CHAIN,
    GENESIS_HASH,
    AuditEvent,
    ChainHead,
    UnauditableEventError,
    canonical,
    link_hash,
    next_record,
)

ACME = UUID(int=1)
AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def an_event(details: dict[str, object]) -> AuditEvent:
    return AuditEvent(tenant_id=ACME, action="tool.called", occurred_at=AT, details=details)


def test_canonical_form_ignores_key_order() -> None:
    assert canonical(an_event({"a": 1, "b": 2})) == canonical(an_event({"b": 2, "a": 1}))


def test_canonical_form_is_compact_sorted_json() -> None:
    assert canonical(an_event({"tool": "sql__query"})) == (
        '{"action":"tool.called","details":{"tool":"sql__query"},'
        '"occurred_at":"2026-11-03T12:00:00+00:00",'
        '"tenant_id":"00000000-0000-0000-0000-000000000001"}'
    )


def test_the_first_record_links_to_the_genesis_hash() -> None:
    record = next_record(EMPTY_CHAIN, an_event({}))

    assert (record.seq, record.prev_hash) == (1, GENESIS_HASH)
    assert record.hash == link_hash(GENESIS_HASH, record.payload)


def test_each_hash_depends_on_the_previous_one() -> None:
    first = next_record(EMPTY_CHAIN, an_event({"n": 1}))
    other_first = next_record(EMPTY_CHAIN, an_event({"n": 99}))
    second = next_record(ChainHead(first.seq, first.hash), an_event({"n": 2}))
    second_after_other = next_record(
        ChainHead(other_first.seq, other_first.hash), an_event({"n": 2})
    )

    assert second.prev_hash == first.hash
    assert second.hash != second_after_other.hash  # same content, different history


def test_link_hash_is_sha256_of_previous_hash_bytes_then_payload() -> None:
    assert link_hash(GENESIS_HASH, "x") == hashlib.sha256(bytes(32) + b"x").hexdigest()


@pytest.mark.parametrize(
    "value",
    ["\ud800", Decimal("1.10"), datetime(2026, 1, 1, tzinfo=UTC), b"bytes", "café"],
    ids=["lone-surrogate", "decimal", "datetime", "bytes", "non-ascii"],
)
def test_awkward_values_still_render_canonically(value: object) -> None:
    rendered = canonical(an_event({"v": value}))

    assert rendered.isascii()
    rendered.encode("utf-8")


def test_an_unrenderable_value_is_a_typed_error() -> None:
    with pytest.raises(UnauditableEventError):
        canonical(an_event({"n": 10**5000}))
