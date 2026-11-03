"""The Cedar engine with the packaged default policies: what the demo servers may do."""

from typing import Any
from uuid import UUID

import pytest

from llm_gateway.authz.cedar_engine import CedarPolicyEngine, PolicyFileError, load_policies
from llm_gateway.policy.model import Decision, PolicyDecision, ToolCall

ACME = UUID(int=1)
NO_PERMIT = ("no policy permits this call",)
FS_READ_TOOLS = [
    "read_text_file",
    "read_multiple_files",
    "list_directory",
    "directory_tree",
    "search_files",
    "get_file_info",
    "list_allowed_directories",
]
FS_WRITE_TOOLS = ["write_file", "edit_file", "create_directory"]


@pytest.fixture(scope="module")
def engine() -> CedarPolicyEngine:
    return CedarPolicyEngine(load_policies(None))


def a_call(server: str, tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(ACME, "acme", server, tool, arguments)


def test_a_select_on_an_allowed_schema_is_allowed(engine: CedarPolicyEngine) -> None:
    decision = engine.decide(a_call("sql", "query", sql="SELECT count(*) FROM analytics.orders"))

    assert decision == PolicyDecision(Decision.ALLOW, ("sql-select-allowed-schemas",))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM billing.cards",
        "SELECT rolpassword FROM pg_authid",
        "DELETE FROM analytics.orders",
        "SELECT 1; DROP TABLE analytics.orders",
        "WITH gone AS (DELETE FROM analytics.orders RETURNING *) SELECT * FROM gone",
        "SELECT dblink_exec('dbname=x', 'DROP TABLE public.t')",
        "SELECT pg_read_file('/app/.env')",
        "not sql at all",
    ],
    ids=[
        "other-schema",
        "unqualified-catalog-table",
        "delete",
        "second-statement",
        "cte-delete",
        "unknown-function",
        "file-reading-function",
        "garbage",
    ],
)
def test_sql_that_is_not_a_plain_read_of_allowed_schemas_is_denied(
    engine: CedarPolicyEngine, sql: str
) -> None:
    assert engine.decide(a_call("sql", "query", sql=sql)) == PolicyDecision(
        Decision.DENY, NO_PERMIT
    )


def test_two_sql_arguments_are_denied(engine: CedarPolicyEngine) -> None:
    call = a_call("sql", "query", query="SELECT 1", sql="DROP TABLE public.t")

    assert engine.decide(call) == PolicyDecision(Decision.DENY, NO_PERMIT)


def test_only_the_query_tool_of_the_sql_server_is_allowed(engine: CedarPolicyEngine) -> None:
    call = a_call("sql", "execute", sql="SELECT 1 FROM analytics.t")

    assert engine.decide(call) == PolicyDecision(Decision.DENY, NO_PERMIT)


@pytest.mark.parametrize("tool", FS_READ_TOOLS)
def test_reading_tools_are_allowed(engine: CedarPolicyEngine, tool: str) -> None:
    assert engine.decide(a_call("fs", tool, path="/data/report.csv")) == PolicyDecision(
        Decision.ALLOW, ("fs-read",)
    )


@pytest.mark.parametrize("tool", FS_WRITE_TOOLS)
def test_writing_inside_the_sandbox_is_allowed(engine: CedarPolicyEngine, tool: str) -> None:
    assert engine.decide(a_call("fs", tool, path="/sandbox/notes.txt")) == PolicyDecision(
        Decision.ALLOW, ("fs-write-sandbox",)
    )


@pytest.mark.parametrize("path", ["/etc/passwd", "/sandbox/../etc/passwd", "/sandboxed/x"])
def test_writing_outside_the_sandbox_is_denied(engine: CedarPolicyEngine, path: str) -> None:
    assert engine.decide(a_call("fs", "write_file", path=path)) == PolicyDecision(
        Decision.DENY, NO_PERMIT
    )


def test_writing_with_no_recognisable_path_is_denied(engine: CedarPolicyEngine) -> None:
    call = a_call("fs", "write_file", target="/sandbox/x")  # not a known path argument

    assert engine.decide(call) == PolicyDecision(Decision.DENY, NO_PERMIT)


def test_an_unsafe_path_is_denied_before_any_policy_runs(engine: CedarPolicyEngine) -> None:
    decision = engine.decide(a_call("fs", "write_file", path="sandbox/x"))

    assert decision.decision is Decision.DENY
    assert decision.reasons[0].startswith("unsafe path argument")


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("write_file", {"path": "/sandbox/.env"}),
        ("read_text_file", {"path": "/app/.ENV"}),
        ("read_text_file", {"path": "/app/.env.local"}),
        ("read_multiple_files", {"paths": ["/data/ok.csv", "/app/.env"]}),
        ("move_file", {"source": "/sandbox/a.env", "destination": "/sandbox/b"}),
        ("move_file", {"source": "/sandbox/a", "destination": "/sandbox/prod.env"}),
    ],
    ids=["write", "upper-case", "env-dot-local", "listed", "move-source", "move-destination"],
)
def test_env_files_are_forbidden_whatever_else_permits_it(
    engine: CedarPolicyEngine, tool: str, arguments: dict[str, Any]
) -> None:
    decision = engine.decide(a_call("fs", tool, **arguments))

    assert decision == PolicyDecision(Decision.DENY, ("fs-never-env-files",))


def test_a_move_inside_the_sandbox_asks_for_approval(engine: CedarPolicyEngine) -> None:
    call = a_call("fs", "move_file", source="/sandbox/a", destination="/sandbox/b")

    assert engine.decide(call) == PolicyDecision(
        Decision.REQUIRE_APPROVAL, ("fs-move-needs-approval",)
    )


@pytest.mark.parametrize(
    ("source", "destination"), [("/sandbox/a", "/etc/a"), ("/etc/a", "/sandbox/a")]
)
def test_a_move_leaving_the_sandbox_is_denied(
    engine: CedarPolicyEngine, source: str, destination: str
) -> None:
    call = a_call("fs", "move_file", source=source, destination=destination)

    assert engine.decide(call) == PolicyDecision(Decision.DENY, NO_PERMIT)


def test_a_tool_no_policy_mentions_is_denied(engine: CedarPolicyEngine) -> None:
    assert engine.decide(a_call("shell", "run", command="rm -rf /")) == PolicyDecision(
        Decision.DENY, NO_PERMIT
    )


def test_any_satisfied_approval_permit_makes_the_call_need_approval() -> None:
    engine = CedarPolicyEngine(
        '@id("plain") permit (principal, action, resource);\n'
        '@id("careful") @decision("require_approval") permit (principal, action, resource);'
    )

    assert engine.decide(a_call("any", "tool")).decision is Decision.REQUIRE_APPROVAL


def test_a_policy_that_errors_denies_instead_of_being_skipped() -> None:
    # Without `has`, reading a missing attribute is an error; Cedar would skip this forbid.
    engine = CedarPolicyEngine(
        "permit (principal, action, resource);\n"
        'forbid (principal, action, resource) when { context.arguments.path like "/etc/*" };'
    )

    decision = engine.decide(a_call("fs", "read_text_file"))

    assert decision.decision is Decision.DENY
    assert decision.reasons[0].startswith("policy evaluation error")


def test_arguments_cedar_can_not_represent_are_denied() -> None:
    engine = CedarPolicyEngine("permit (principal, action, resource);")

    decision = engine.decide(a_call("any", "tool", blob=b"\x00\x01"))

    assert decision.decision is Decision.DENY
    assert decision.reasons[0].startswith("policy engine error")


def test_non_ascii_tool_names_are_judged_like_any_other() -> None:
    engine = CedarPolicyEngine(
        'permit (principal, action, resource) when { resource.server == "fsé" };'
    )

    assert engine.decide(a_call("fsé", "lire")).decision is Decision.ALLOW


def test_policies_can_name_the_tenant() -> None:
    engine = CedarPolicyEngine(
        'permit (principal, action, resource) when { principal.name == "acme" };'
    )

    assert engine.decide(a_call("any", "tool")).decision is Decision.ALLOW
    other = ToolCall(UUID(int=2), "globex", "any", "tool", {})
    assert engine.decide(other).decision is Decision.DENY


@pytest.mark.parametrize(
    "policies",
    [
        "permit (principal, action, resource",
        "",
        '@decision("REQUIRE_APPROVAL") permit (principal, action, resource);',
        '@decision("require_approval") forbid (principal, action, resource);',
        '@id("a") permit (principal, action, resource);\n'
        '@id("a") permit (principal, action, resource);',
    ],
    ids=["syntax", "empty", "unknown-decision", "decision-on-forbid", "duplicate-id"],
)
def test_unusable_policy_files_are_refused_at_load(policies: str) -> None:
    with pytest.raises(PolicyFileError):
        CedarPolicyEngine(policies)
