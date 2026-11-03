"""Facts derived from a tool call's arguments, so policies judge meaning, not raw text.

Raw arguments are easy to game: `/sandbox/../etc/passwd` matches `like "/sandbox/*"`, and
`SELECT 1; DROP TABLE t` starts with SELECT. Policies read these facts instead:

    context.paths.<argument>   a single path argument, normalized ("/etc/passwd")
    context.path_count         how many paths the call names, in all path arguments
    context.path_roots         their top-level directories, e.g. ["/sandbox"]
    context.path_extensions    their lowercased extensions, e.g. [".env", ".txt"]
    context.sql                {parsed, only_select, statements, tables, schemas,
                                unknown_functions}

A path that can't be judged safely (relative, or with control characters, backslashes or
surrounding spaces) is refused before any policy runs: UnsafeArgumentError.

Cedar's `like` only matches single strings, so rules about "every path" use the sets:
`["/sandbox"].containsAll(context.path_roots)`, `context.path_extensions.contains(".env")`.
"""

import posixpath
import re
from collections.abc import Mapping
from typing import Any

import sqlglot
from sqlglot import exp

# Argument names that carry a filesystem path, and those that carry SQL.
PATH_ARGUMENTS = frozenset(
    {"path", "paths", "file", "files", "filename", "directory", "dir", "source", "destination"}
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


class UnsafeArgumentError(ValueError):
    """An argument the gateway can't judge safely; the call is denied without asking Cedar."""


def derive_facts(arguments: Mapping[str, Any]) -> dict[str, Any]:
    every_path = _every_path(arguments)
    facts: dict[str, Any] = {
        "paths": _paths(arguments),
        "path_count": len(every_path),
        "path_roots": sorted({_root(p) for p in every_path}),
        "path_extensions": sorted({e for p in every_path for e in _extensions(p)}),
    }
    sql_values = [v for k, v in arguments.items() if k in SQL_ARGUMENTS]
    if sql_values:
        # Exactly one SQL string, or nothing a policy can trust: which one would the server run?
        single = len(sql_values) == 1 and isinstance(sql_values[0], str)
        facts["sql"] = sql_facts(sql_values[0]) if single else _UNPARSED
    return facts


def normalized_path(path: str) -> str:
    """Absolute, with `.`/`..`/`//` collapsed. Raises UnsafeArgumentError for anything else:
    a relative path resolves against the server's working directory, which we can't see."""
    if not path.startswith("/") or path != path.strip() or _UNSAFE_PATH_CHARS.search(path):
        raise UnsafeArgumentError(f"unsafe path argument {path!r}")
    return posixpath.normpath(path)


def sql_facts(sql: str) -> dict[str, Any]:
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except Exception:
        return _UNPARSED
    tables = sorted({_table_name(t) for s in statements for t in _real_tables(s)})
    return {
        "parsed": bool(statements),
        "only_select": bool(statements) and all(_is_read_only(s) for s in statements),
        "statements": len(statements),
        "tables": tables,
        "schemas": sorted({name.split(".")[0] for name in tables}),
        # Functions sqlglot doesn't know (dblink_exec, pg_read_file, setval, my_schema.f…):
        # a SELECT can still do anything through them.
        "unknown_functions": sorted(
            {f.name.lower() for s in statements for f in s.find_all(exp.Anonymous)}
        ),
    }


def _every_path(arguments: Mapping[str, Any]) -> list[str]:
    """Every path in every path argument, single or listed, normalized."""
    found: list[str] = []
    for key, value in arguments.items():
        if key not in PATH_ARGUMENTS:
            continue
        values = value if isinstance(value, list | tuple) else [value]
        found.extend(normalized_path(v) for v in values if isinstance(v, str))
    return found


def _root(path: str) -> str:
    return "/" + path.lstrip("/").split("/", 1)[0]


def _extensions(path: str) -> list[str]:
    """Every dotted part of the name, lowercased: ".env.local" and "prod.ENV.bak" both give
    ".env" (dotfiles count as their own extension)."""
    return ["." + part.lower() for part in posixpath.basename(path).split(".")[1:] if part]


_UNSAFE_PATH_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")
UNQUALIFIED_SCHEMA = "(unqualified)"
_UNPARSED: dict[str, Any] = {
    "parsed": False,
    "only_select": False,
    "statements": 0,
    "tables": [],
    "schemas": [],
    "unknown_functions": [],
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
    # An unqualified name resolves through search_path (pg_catalog first, then "$user"),
    # so it can't be attributed to any one schema.
    return f"{table.db or UNQUALIFIED_SCHEMA}.{table.name}"
