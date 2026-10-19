"""Generic runtime for tools registered by integrations and MCP plugins."""
from __future__ import annotations

from typing import Any

from app.services.operator_policy import ToolMutationGate
from app.services.platform_capabilities import (
    ClientAuthorizationService, allowed_capabilities, require_capability,
)
from app.services.platform_services import (
    JobService, PlatformOperationError, PlatformStore,
)
from app.services.tool_models import CoreToolDefinition


class CoreToolRuntime:
    """An empty-by-default tool registry with shared authorization checks."""

    def __init__(self, *, store: PlatformStore | None = None) -> None:
        self.store = store or PlatformStore()
        self.jobs = JobService(self.store)
        self.clients = ClientAuthorizationService(self.store)
        self.mutation_gate = ToolMutationGate()
        self._tools: dict[str, CoreToolDefinition] = {}
        self._integration_mcp = None

    def register_tool(self, definition: CoreToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"Duplicate tool registration: {definition.name}")
        self._tools[definition.name] = definition

    def definitions(self) -> list[CoreToolDefinition]:
        return list(self._tools.values())

    def get_definition(self, name: str) -> CoreToolDefinition | None:
        return self._tools.get(name)

    def list_definitions(self, actor: Any = None) -> list[dict[str, Any]]:
        granted = allowed_capabilities(actor) if actor is not None else frozenset()
        result = []
        for definition in self.definitions():
            entry = definition.metadata()
            entry["authorized"] = definition.capability in granted
            result.append(entry)
        return result

    def invoke(
        self, name: str, actor: Any, arguments: dict[str, Any] | None = None,
        *, policy_context: dict[str, Any] | None = None,
    ) -> Any:
        definition = self._tools.get(name)
        if definition is None:
            raise PlatformOperationError("Unknown registered tool.")
        require_capability(actor, definition.capability)
        decision = self.mutation_gate.decide_tool(
            tool_name=name, risk_class=definition.risk_class,
            capability=definition.capability,
            actor_capabilities=set(allowed_capabilities(actor)),
            autonomous_actor=True,
            approved=bool((policy_context or {}).get("approved_action")),
            context=policy_context,
        )
        if decision.decision != "allow":
            raise PlatformOperationError(
                f"Policy decision for {name}: {decision.decision} ({'; '.join(decision.reasons)})."
            )
        return definition.handler(actor, arguments or {})
