"""What the MCP aggregator needs from a policy engine."""

from typing import Protocol

from llm_gateway.policy.model import PolicyDecision, ToolCall


class PolicyEngine(Protocol):
    def decide(self, call: ToolCall) -> PolicyDecision:
        """Allow, deny or require approval. Must fail closed: any doubt is DENY."""
        ...
