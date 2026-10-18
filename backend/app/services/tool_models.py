"""Transport-neutral data model for the Lynewright Tool Runtime.

This module owns the vocabulary every tool caller shares — external MCP
agents, the internal Operator, and future transports. It deliberately has no
imports from ``mcp_server`` or any transport so both directions stay clean:

    MCP transport ─┐
                   ├─ CoreToolRuntime ─ platform services
    Operator ──────┘
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

MAX_TOOL_INPUT_BYTES = 64 * 1024

# Shared risk classes (master plan §Shared Risk Classes). The runtime records
# them as metadata; enforcement beyond capability checks belongs to the policy
# session (G), which may tighten these values without a schema change.
RISK_CLASSES = frozenset({
    "READ_ONLY",
    "SAFE_OPERATION",
    "BOUNDED_CONFIG",
    "PUBLICATION",
    "MONEY_MOVING",
    "DESTRUCTIVE",
    "SECURITY_SENSITIVE",
})

# Conservative default for tools whose semantics the runtime cannot know
# (integration contributions): capability-checked, reversible-by-contract.
DEFAULT_RISK_CLASS = "SAFE_OPERATION"


@dataclass(frozen=True, slots=True)
class CoreToolDefinition:
    """One callable Lynewright tool with the metadata an agent needs.

    ``handler`` receives ``(actor, arguments)`` and returns any JSON-safe
    value. Everything except name/capability/handler is advisory metadata that
    transports may project (MCP annotations) or agents may use for planning.
    """

    name: str
    capability: str
    handler: Callable[[Any, dict[str, Any]], Any]
    description: str = ""
    read_only: bool = False
    destructive: bool = False
    risk_class: str = DEFAULT_RISK_CLASS
    input_schema: dict[str, Any] | None = None
    # Provider-neutral result contract.  These fields are optional so older
    # integrations remain callable, while new integrations can participate in
    # deterministic routing and typed bindings without a provider-specific
    # adapter.
    output_schema: dict[str, Any] | None = None
    input_semantics: dict[str, Any] | None = None
    output_semantics: dict[str, Any] | None = None
    completion_contract: dict[str, Any] | None = None
    artifact_contract: dict[str, Any] | None = None
    contract_status: str = "declared"
    source: str = "core"  # "core" | "integration"
    # Whether a successful transport response may explicitly declare that the
    # requested remote operation reached a terminal failure state.  This is
    # metadata, not a provider-specific rule; command tools such as cancel
    # can opt out because ``status=cancelled`` is their successful result.
    reports_terminal_state: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Tool definition requires a name.")
        if not self.capability:
            raise ValueError(f"Tool {self.name} requires a capability.")
        if self.risk_class not in RISK_CLASSES:
            raise ValueError(f"Tool {self.name} has unknown risk class {self.risk_class}.")
        if self.read_only and self.destructive:
            raise ValueError(f"Tool {self.name} cannot be both read-only and destructive.")
        if not callable(self.handler):
            raise ValueError(f"Tool {self.name} handler must be callable.")
        if self.contract_status not in {"declared", "verified", "deprecated"}:
            raise ValueError(f"Tool {self.name} has unknown contract status {self.contract_status}.")

    def metadata(self) -> dict[str, Any]:
        """Structured metadata safe to expose to any agent."""
        return {
            "name": self.name,
            "description": self.description,
            "capability": self.capability,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "risk_class": self.risk_class,
            "input_schema": dict(self.input_schema) if self.input_schema else None,
            "output_schema": dict(self.output_schema) if self.output_schema else None,
            "input_semantics": dict(self.input_semantics) if self.input_semantics else None,
            "output_semantics": dict(self.output_semantics) if self.output_semantics else None,
            "completion_contract": dict(self.completion_contract) if self.completion_contract else None,
            "artifact_contract": dict(self.artifact_contract) if self.artifact_contract else None,
            "contract_status": self.contract_status,
            "source": self.source,
            "reports_terminal_state": self.reports_terminal_state,
        }


# Backwards-compatible name for code that predated the runtime extraction.
MCPToolDefinition = CoreToolDefinition
