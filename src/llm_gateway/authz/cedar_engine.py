"""PolicyEngine backed by Cedar (cedarpy), evaluated in-process.

Request shape, so policies can be written against it:

    principal  Tenant::"<tenant id>"       attrs: name
    action     Action::"call_tool"
    resource   Tool::"<server>__<tool>"   attrs: server, name (the tool's own name)
    context    arguments: the raw arguments
               paths:     normalized path arguments     (see policy/facts.py)
               sql:       parsed SQL facts, when there is a sql/query argument

Cedar itself only says Allow or Deny. A permit annotated `@decision("require_approval")`
turns its Allow into REQUIRE_APPROVAL.
"""

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import cedarpy

from llm_gateway.policy.facts import UnsafeArgumentError, derive_facts
from llm_gateway.policy.model import Decision, PolicyDecision, ToolCall


class PolicyFileError(ValueError):
    """The Cedar policies don't parse; the gateway refuses to start with them."""


def load_policies(path: str | None) -> str:
    """The policy file at `path`, or the packaged defaults when no path is set."""
    if path is None:
        return files("llm_gateway.authz").joinpath("default.cedar").read_text(encoding="utf-8")
    return Path(path).read_text(encoding="utf-8")


class CedarPolicyEngine:
    def __init__(self, policies: str) -> None:
        try:
            parsed = json.loads(cedarpy.policies_to_json_str(policies))
        except (ValueError, RuntimeError) as error:
            raise PolicyFileError(str(error)) from error
        static = parsed.get("staticPolicies", {})
        if not static:
            raise PolicyFileError("no policies: every tool call would be denied")
        _check_annotations(static)
        self._policies = policies
        # Cedar names policies policy0, policy1… by position; @id gives them readable names.
        self._names = {pid: p.get("annotations", {}).get("id", pid) for pid, p in static.items()}
        self._needs_approval = {
            pid
            for pid, p in static.items()
            if p.get("annotations", {}).get("decision") == Decision.REQUIRE_APPROVAL
        }

    @property
    def policy_names(self) -> list[str]:
        return sorted(self._names.values())

    def decide(self, call: ToolCall) -> PolicyDecision:
        try:
            request = _request(call)
        except UnsafeArgumentError as error:
            return PolicyDecision(Decision.DENY, (str(error),))
        try:
            result = cedarpy.is_authorized(request, self._policies, _entities(call))
        except Exception as error:
            return PolicyDecision(Decision.DENY, (f"policy engine error: {type(error).__name__}",))
        errors = list(result.diagnostics.errors)
        if errors:
            # A policy that errors is skipped by Cedar; a skipped forbid must not mean allow.
            return PolicyDecision(Decision.DENY, ("policy evaluation error: " + str(errors[0]),))
        reasons = tuple(sorted(self._names.get(r, r) for r in result.diagnostics.reasons))
        if result.decision != cedarpy.Decision.Allow:
            return PolicyDecision(Decision.DENY, reasons or ("no policy permits this call",))
        if any(r in self._needs_approval for r in result.diagnostics.reasons):
            return PolicyDecision(Decision.REQUIRE_APPROVAL, reasons)
        return PolicyDecision(Decision.ALLOW, reasons)


def _request(call: ToolCall) -> dict[str, Any]:
    return {
        "principal": f'Tenant::"{call.tenant_id}"',
        "action": 'Action::"call_tool"',
        "resource": f"Tool::{json.dumps(call.qualified_name, ensure_ascii=False)}",
        "context": {"arguments": dict(call.arguments)} | derive_facts(call.arguments),
    }


def _entities(call: ToolCall) -> list[dict[str, Any]]:
    return [
        {
            "uid": {"type": "Tenant", "id": str(call.tenant_id)},
            "attrs": {"name": call.tenant_name},
            "parents": [],
        },
        {
            "uid": {"type": "Tool", "id": call.qualified_name},
            "attrs": {"server": call.server, "name": call.tool},
            "parents": [],
        },
    ]


def _check_annotations(static: dict[str, Any]) -> None:
    """Annotation mistakes would otherwise change decisions silently."""
    seen_ids: set[str] = set()
    for pid, policy in static.items():
        annotations = policy.get("annotations", {})
        name = annotations.get("id", pid)
        if name in seen_ids:
            raise PolicyFileError(f"two policies share @id({name!r})")
        seen_ids.add(name)
        decision = annotations.get("decision")
        if decision is None:
            continue
        if decision != Decision.REQUIRE_APPROVAL:
            raise PolicyFileError(f"{name}: unknown @decision({decision!r})")
        if policy.get("effect") != "permit":
            raise PolicyFileError(f"{name}: @decision only applies to permit policies")
