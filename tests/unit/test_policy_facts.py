import pytest

from llm_gateway.policy.facts import (
    UNQUALIFIED_SCHEMA,
    UnsafeArgumentError,
    derive_facts,
    normalized_path,
    sql_facts,
)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("/sandbox/a.txt", "/sandbox/a.txt"),
        ("/sandbox/../etc/passwd", "/etc/passwd"),
        ("/sandbox/./x//y", "/sandbox/x/y"),
        ("/../../etc/shadow", "/etc/shadow"),
    ],
)
def test_paths_are_normalized_before_policies_see_them(raw: str, normalized: str) -> None:
    assert normalized_path(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    [
        "sandbox/x",
        "../sandbox/x",
        "/sandbox/x\x00",
        "/sandbox/x\n",
        "/app/.env ",
        " /etc",
        "/a\\..\\b",
    ],
    ids=[
        "relative",
        "dot-dot-relative",
        "nul",
        "newline",
        "trailing-space",
        "leading-space",
        "backslash",
    ],
)
def test_paths_that_can_not_be_judged_safely_are_refused(raw: str) -> None:
    with pytest.raises(UnsafeArgumentError):
        normalized_path(raw)


def test_only_path_arguments_are_normalized() -> None:
    facts = derive_facts({"path": "/a/../b", "content": "/a/../b", "destination": "/x"})

    assert facts["paths"] == {"path": "/b", "destination": "/x"}


def test_every_path_in_every_path_argument_feeds_the_path_sets() -> None:
    facts = derive_facts({"paths": ["/sandbox/a.TXT", "/app/.env.local"], "source": "/var/x"})

    assert facts["path_count"] == 3
    assert facts["path_roots"] == ["/app", "/sandbox", "/var"]
    assert facts["path_extensions"] == [".env", ".local", ".txt"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM analytics.orders",
        "select id from analytics.users where name = 'DROP TABLE x'",
        "SELECT 1 UNION SELECT 2",
        "/* note */ SELECT 1 -- trailing",
    ],
)
def test_plain_reads_are_select_only(sql: str) -> None:
    assert sql_facts(sql)["only_select"]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM analytics.orders",
        "SELECT 1; DROP TABLE analytics.orders",
        "WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone",
        "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
        "WITH x AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM x",
        "SELECT * INTO stolen FROM orders",
        "COPY orders TO '/tmp/out.csv'",
        "SELEC typo",
        "",
        "SELECT " + "(" * 60 + "1" + ")" * 60,
    ],
    ids=[
        "delete",
        "second-statement",
        "cte-delete",
        "cte-insert",
        "cte-update",
        "select-into",
        "copy",
        "garbage",
        "empty",
        "too-deep-to-parse",
    ],
)
def test_anything_that_writes_or_can_not_be_read_is_not_select_only(sql: str) -> None:
    assert not sql_facts(sql)["only_select"]


def test_schemas_and_tables_are_listed_without_cte_names() -> None:
    facts = sql_facts(
        "WITH recent AS (SELECT * FROM analytics.orders) SELECT * FROM recent, public.users"
    )

    assert facts["tables"] == ["analytics.orders", "public.users"]
    assert facts["schemas"] == ["analytics", "public"]
    assert facts["statements"] == 1


def test_unqualified_tables_are_not_attributed_to_any_schema() -> None:
    # Postgres resolves pg_authid through pg_catalog, not public.
    assert sql_facts("SELECT rolpassword FROM pg_authid")["schemas"] == [UNQUALIFIED_SCHEMA]


def test_functions_the_parser_does_not_know_are_listed() -> None:
    facts = sql_facts("SELECT count(*), dblink_exec('x', 'DROP TABLE t'), Pg_Read_File('/x')")

    assert facts["unknown_functions"] == ["dblink_exec", "pg_read_file"]


@pytest.mark.parametrize(
    "arguments",
    [{"query": "SELECT 1", "sql": "DROP TABLE t"}, {"sql": ["SELECT 1"]}],
    ids=["two-sql-arguments", "not-a-string"],
)
def test_sql_a_policy_can_not_trust_is_unparsed(arguments: dict[str, object]) -> None:
    assert derive_facts(arguments)["sql"]["parsed"] is False


def test_sql_facts_appear_only_for_sql_arguments() -> None:
    assert "sql" in derive_facts({"query": "SELECT 1"})
    assert "sql" not in derive_facts({"path": "/x"})
