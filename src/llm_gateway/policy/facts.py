"""Facts derived from a tool call's arguments, so policies judge meaning, not raw text.

Raw arguments are easy to game: `/sandbox/../etc/passwd` matches `like "/sandbox/*"`, and
`SELECT 1; DROP TABLE t` starts with SELECT. Policies read these facts instead:

    context.paths.<argument>   the path, normalized ("/etc/passwd")
    context.sql                {parsed, only_select, statements, tables, schemas}
"""

import posixpath
from collections.abc import Mapping
from typing import Any

import sqlglot
from sqlglot import exp

# Argument names that carry a filesystem path, and those that carry SQL.
PATH_ARGUMENTS = frozenset(
    {"path", "file", "filename", "directory", "dir", "source", "destination"}
)
SQL_ARGUMENTS = frozenset({"sql", "query"})

_READ_ROOTS = (exp.Select, exp.SetOperation)  # SELECT, and UNION/INTERSECT/EXCEPT of SELECTs
_WRITES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,  # anything sqlglot couldn't classify
    exp.Copy,
)


def derive_facts(arguments: Mapping[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {"paths": _paths(arguments)}
    sql = next((v for k, v in arguments.items() if k in SQL_ARGUMENTS and isinstance(v, str)), None)
    if sql is not None:
        facts["sql"] = sql_facts(sql)
    return facts


def normalized_path(path: str) -> str:
    """Absolute, with `.`/`..`/`//` collapsed. A relative path is anchored at `/`."""
    return posixpath.normpath(posixpath.join("/", path))


def sql_facts(sql: str) -> dict[str, Any]:
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.SqlglotError:
        return {"parsed": False, "only_select": False, "statements": 0, "tables": [], "schemas": []}
    tables = sorted({_table_name(t) for s in statements for t in _real_tables(s)})
    return {
        "parsed": bool(statements),
        "only_select": bool(statements) and all(_is_read_only(s) for s in statements),
        "statements": len(statements),
        "tables": tables,
        "schemas": sorted({name.split(".")[0] for name in tables}),
    }


def _paths(arguments: Mapping[str, Any]) -> dict[str, str]:
    return {
        key: normalized_path(value)
        for key, value in arguments.items()
        if key in PATH_ARGUMENTS and isinstance(value, str)
    }


def _is_read_only(statement: exp.Expr) -> bool:
    if not isinstance(statement, _READ_ROOTS):
        return False
    if any(isinstance(node, _WRITES) for node in statement.walk()):
        return False
    # SELECT … INTO creates a table.
    return not any(
        isinstance(node, exp.Select) and node.args.get("into") for node in statement.walk()
    )


def _real_tables(statement: exp.Expr) -> list[exp.Table]:
    cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
    return [t for t in statement.find_all(exp.Table) if t.db or t.name not in cte_names]


def _table_name(table: exp.Table) -> str:
    # Unqualified names resolve through the default search path: public.
    return f"{table.db or 'public'}.{table.name}"
