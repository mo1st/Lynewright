"""Controlled projection of opt-in integration tools into Lynewright MCP."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.integration_contributions import Contribution, ContributionRegistry
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.platform_capabilities import CapabilityActor, require_capability
from app.services.platform_services import PlatformConflictError, PlatformOperationError, PlatformValidationError
from app.services.tool_models import RISK_CLASSES


@dataclass(frozen=True, slots=True)
class MCPContributionDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    caller_capability: str
    integration_capability: str
    project_scope: str
    contribution_id: str
    risk_class: str
    read_only: bool
    # Optional provider-neutral contracts are carried through the MCP
    # projection so internal Operator calls and external MCP callers discover
    # the same typed output/binding metadata.
    output_schema: dict[str, Any] | None = None
    input_semantics: dict[str, Any] | None = None
    output_semantics: dict[str, Any] | None = None
    completion_contract: dict[str, Any] | None = None
    artifact_contract: dict[str, Any] | None = None
    contract_status: str = "declared"


class IntegrationMCPContributionBridge:
    """Maps declared ``mcp`` contributions to MCP registry definitions.

    The bridge is intentionally one-way.  It never discovers or re-exposes
    tools from an external MCP server; an integration must explicitly register
    each domain tool through ``ContributionRegistry``.
    """

    def __init__(self, contributions: ContributionRegistry, sdk_factory: IntegrationSDKFactory) -> None:
        self.contributions = contributions
        self.sdk_factory = sdk_factory

    def definitions(self) -> list[MCPContributionDefinition]:
        values: list[MCPContributionDefinition] = []
        names: set[str] = set()
        for contribution in self.contributions.list(kind="mcp"):
            definition = self._definition(contribution)
            if definition.name in names:
                raise PlatformConflictError("Duplicate integration MCP tool name.")
            names.add(definition.name)
            values.append(definition)
        return values

    def invoke(
        self,
        contribution_id: str,
        actor: CapabilityActor,
        arguments: dict[str, Any],
        *,
        invocation_context: Any | None = None,
    ) -> Any:
        contribution = self.contributions.get(contribution_id)
        definition = self._definition(contribution)
        require_capability(actor, definition.caller_capability)
        project_id = arguments.get("project_id")
        if project_id is not None and not isinstance(project_id, str):
            raise PlatformValidationError("MCP contribution project_id must be a string.")
        if definition.project_scope == "required" and not project_id:
            raise PlatformOperationError("This integration MCP tool requires a project_id.")
        if definition.project_scope == "none" and project_id:
            raise PlatformOperationError("This integration MCP tool is not project-scoped.")

        integration = self.sdk_factory.authorization.resolve_actor(contribution.integration_id)
        require_capability(integration, definition.integration_capability)  # Integration grant, not caller grant.
        if project_id:
            if self.sdk_factory.project_scope is None:
                raise PlatformOperationError("Project-scoped integration MCP tools require a project-scope policy.")
            if not self.sdk_factory.project_scope(actor, integration, project_id):
                raise PlatformOperationError("Project scope denied this integration MCP tool.")
        if self.sdk_factory.platform_policy is not None and not self.sdk_factory.platform_policy(
            actor, integration, definition.integration_capability, project_id or None,
        ):
            raise PlatformOperationError("Platform policy denied this integration MCP tool.")

        sdk = self.sdk_factory.create(
            caller=actor, integration_id=contribution.integration_id,
            connection_id=contribution.connection_id, project_id=project_id or None,
            invocation_context=invocation_context,
        )
        try:
            return self.contributions.invoke(
                contribution_id, sdk, arguments, invocation_context=invocation_context,
            )
        except PlatformOperationError:
            raise
        except Exception as exc:
            raise PlatformOperationError("Integration MCP tool failed.") from exc

    @staticmethod
    def _definition(contribution: Contribution) -> MCPContributionDefinition:
        metadata = contribution.metadata
        read_only = bool(metadata.get("read_only", False))
        declared_risk = str(metadata.get("risk_class") or "PUBLICATION")
        risk_class = "READ_ONLY" if read_only else (declared_risk if declared_risk in RISK_CLASSES else "PUBLICATION")
        contract_status = str(metadata.get("contract_status") or "declared")
        if contract_status not in {"declared", "verified", "deprecated"}:
            contract_status = "declared"
        return MCPContributionDefinition(
            name=str(metadata["name"]), description=str(metadata["description"]),
            input_schema=dict(metadata["input_schema"]), caller_capability=str(metadata["caller_capability"]),
            integration_capability=str(metadata["integration_capability"]), project_scope=str(metadata["project_scope"]),
            contribution_id=contribution.id,
            # Integration-originated tools are not assumed safe merely because
            # the provider declared them.  They require Operator approval by
            # default; an integration may explicitly mark a read-only tool.
            risk_class=risk_class,
            read_only=read_only,
            output_schema=dict(metadata["output_schema"]) if isinstance(metadata.get("output_schema"), dict) else None,
            input_semantics=dict(metadata["input_semantics"]) if isinstance(metadata.get("input_semantics"), dict) else None,
            output_semantics=dict(metadata["output_semantics"]) if isinstance(metadata.get("output_semantics"), dict) else None,
            completion_contract=dict(metadata["completion_contract"]) if isinstance(metadata.get("completion_contract"), dict) else None,
            artifact_contract=dict(metadata["artifact_contract"]) if isinstance(metadata.get("artifact_contract"), dict) else None,
            contract_status=contract_status,
        )
