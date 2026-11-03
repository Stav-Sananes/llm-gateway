"""The Cedar engine with the packaged default policies: what the demo servers may do."""

from typing import Any
from uuid import UUID

import pytest

from llm_gateway.authz.cedar_engine import CedarPolicyEngine, PolicyFileError, load_policies
from llm_gateway.policy.model import Decision, ToolCall

ACME = UUID(int=1)


@pytest.fixture(scope="module")
def engine() -> CedarPolicyEngine:
    return CedarPolicyEngine(load_policies(None))


def a_call(server: str, tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(ACME, "acme", server, tool, arguments)


def decide(engine: CedarPolicyEngine, call: ToolCall) -> Decision:
    return engine.decide(call).decision


def test_a_select_on_an_allowed_schema_is_allowed(engine: CedarPolicyEngine) -> None:
    decision = engine.decide(a_call("sql", "query", sql="SELECT * FROM analytics.orders"))

    assert decision.decision is Decision.ALLOW
    assert decision.reasons == ("sql-select-allowed-schemas",)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM billing.cards",
        "DELETE FROM analytics.orders",
        "SELECT 1; DROP TABLE analytics.orders",
        "WITH gone AS (DELETE FROM analytics.orders RETURNING *) SELECT * FROM gone",
        "not sql at all",
    ],
    ids=["other-schema", "delete", "second-statement", "cte-delete", "garbage"],
)
def test_sql_that_is_not_a_plain_read_of_allowed_schemas_is_denied(
    engine: CedarPolicyEngine, sql: str
) -> None:
    assert decide(engine, a_call("sql", "query", sql=sql)) is Decision.DENY


def test_writing_inside_the_sandbox_is_allowed(engine: CedarPolicyEngine) -> None:
    assert decide(engine, a_call("fs", "write_file", path="/sandbox/notes.txt")) is Decision.ALLOW


@pytest.mark.parametrize("path", ["/etc/passwd", "/sandbox/../etc/passwd", "/sandboxed/x"])
def test_writing_outside_the_sandbox_is_denied(engine: CedarPolicyEngine, path: str) -> None:
    assert decide(engine, a_call("fs", "write_file", path=path)) is Decision.DENY


def test_a_forbid_beats_every_permit(engine: CedarPolicyEngine) -> None:
    decision = engine.decide(a_call("fs", "write_file", path="/sandbox/.env"))

    assert decision.decision is Decision.DENY
    assert decision.reasons == ("fs-never-env-files",)


def test_an_annotated_permit_asks_for_approval(engine: CedarPolicyEngine) -> None:
    call = a_call("fs", "move_file", source="/sandbox/a", destination="/sandbox/b")

    assert decide(engine, call) is Decision.REQUIRE_APPROVAL


def test_a_tool_no_policy_mentions_is_denied(engine: CedarPolicyEngine) -> None:
    decision = engine.decide(a_call("shell", "run", command="rm -rf /"))

    assert decision.decision is Decision.DENY
    assert decision.reasons == ("no policy permits this call",)


def test_a_policy_that_errors_denies_instead_of_being_skipped() -> None:
    # Without `has`, reading a missing attribute is an error; Cedar would skip this forbid.
    engine = CedarPolicyEngine(
        "permit (principal, action, resource);\n"
        'forbid (principal, action, resource) when { context.arguments.path like "/etc/*" };'
    )

    decision = engine.decide(a_call("fs", "read_text_file"))

    assert decision.decision is Decision.DENY
    assert decision.reasons[0].startswith("policy evaluation error")


def test_policies_can_name_the_tenant() -> None:
    engine = CedarPolicyEngine(
        'permit (principal, action, resource) when { principal.name == "acme" };'
    )

    assert decide(engine, a_call("any", "tool")) is Decision.ALLOW
    other = ToolCall(UUID(int=2), "globex", "any", "tool", {})
    assert decide(engine, other) is Decision.DENY


@pytest.mark.parametrize(
    "policies", ["permit (principal, action, resource", ""], ids=["syntax", "empty"]
)
def test_unusable_policy_files_are_refused_at_startup(policies: str) -> None:
    with pytest.raises(PolicyFileError):
        CedarPolicyEngine(policies)
