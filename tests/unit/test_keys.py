import re

from llm_gateway.tenancy.keys import hash_key, issue_key, looks_like_key

HEX_64 = re.compile(r"[0-9a-f]{64}")


def test_issued_key_is_gw_then_prefix_then_secret() -> None:
    issued = issue_key()

    # maxsplit=2: the secret itself may contain "_", the prefix never does
    scheme, prefix, secret = issued.plaintext.split("_", 2)

    assert scheme == "gw"
    assert prefix == issued.prefix
    assert re.fullmatch(r"[0-9a-f]{8}", issued.prefix)
    assert secret


def test_each_issued_key_is_unique() -> None:
    keys = {issue_key().plaintext for _ in range(100)}

    assert len(keys) == 100


def test_issued_key_hash_matches_hashing_its_plaintext() -> None:
    issued = issue_key()

    assert issued.key_hash == hash_key(issued.plaintext)
    assert HEX_64.fullmatch(issued.key_hash)


def test_hash_key_is_sha256_hex() -> None:
    # Known SHA-256 test vector: pins the algorithm, not just "some hash".
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

    assert hash_key("abc") == expected


def test_hash_does_not_reveal_the_secret() -> None:
    issued = issue_key()
    secret = issued.plaintext.split("_", 2)[2]

    assert secret not in issued.key_hash


def test_issued_keys_have_the_key_shape() -> None:
    assert all(looks_like_key(issue_key().plaintext) for _ in range(100))


def test_other_strings_do_not_have_the_key_shape() -> None:
    key = issue_key().plaintext

    assert not looks_like_key("")
    assert not looks_like_key("sk-" + key[3:])
    assert not looks_like_key(key[:-1])
    assert not looks_like_key(key + "x")
