import pytest

from llm_gateway.policy.facts import derive_facts, normalized_path, sql_facts


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("/sandbox/a.txt", "/sandbox/a.txt"),
        ("/sandbox/../etc/passwd", "/etc/passwd"),
        ("/sandbox/./x//y", "/sandbox/x/y"),
        ("sandbox/a.txt", "/sandbox/a.txt"),
        ("../../etc/shadow", "/etc/shadow"),
    ],
)
def test_paths_are_normalized_before_policies_see_them(raw: str, normalized: str) -> None:
    assert normalized_path(raw) == normalized


def test_only_path_arguments_are_normalized() -> None:
    facts = derive_facts({"path": "/a/../b", "content": "/a/../b", "destination": "x"})

    assert facts["paths"] == {"path": "/b", "destination": "/x"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM analytics.orders",
        "select id from users where name = 'DROP TABLE x'",
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
        "SELECT * INTO stolen FROM orders",
        "COPY orders TO '/tmp/out.csv'",
        "SELEC typo",
        "",
    ],
    ids=["delete", "second-statement", "cte-delete", "select-into", "copy", "garbage", "empty"],
)
def test_anything_that_writes_or_can_not_be_read_is_not_select_only(sql: str) -> None:
    assert not sql_facts(sql)["only_select"]


def test_schemas_and_tables_are_listed_without_cte_names() -> None:
    facts = sql_facts("WITH recent AS (SELECT * FROM analytics.orders) SELECT * FROM recent, users")

    assert facts["tables"] == ["analytics.orders", "public.users"]
    assert facts["schemas"] == ["analytics", "public"]


def test_sql_facts_appear_only_for_sql_arguments() -> None:
    assert "sql" in derive_facts({"query": "SELECT 1"})
    assert "sql" not in derive_facts({"path": "/x"})
