"""Project-scoped Operator projection of configured MCP contract mappings.

Only transport and contract metadata are interpreted here, never provider names.
The approved mapping fingerprint is part of the tool identity: changing its
meaning invalidates pending tool calls rather than reusing an old approval.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.services.capability_context import CapabilityInvocationContext
from app.services.platform_capabilities import allowed_capabilities, require_capability
from app.services.platform_services import PlatformOperationError, PlatformServiceError
from app.services.tool_models import CoreToolDefinition


class OperatorMappedTools:
    def __init__(self, management: Any):
        self.management = management
        from app.services.integration_operations import IntegrationOperationCatalog
        self.operation_catalog = IntegrationOperationCatalog(management)

    def project(self, actor: Any, requested: str = "") -> str:
        prefix = "internal-operator:"
        if not actor.client_id.startswith(prefix):
            raise PlatformOperationError("Mapped Operator tools require an Operator profile.")
        with self.management.store.connection() as db:
            row = db.execute(
                "SELECT tenant_id,project_id,enabled FROM operator_profiles WHERE id=?",
                (actor.client_id[len(prefix):],),
            ).fetchone()
        if row is None or not row["enabled"] or row["tenant_id"] != self.management.tenant_id:
            raise PlatformOperationError("Operator profile is unavailable in this tenant.")
        project = str(row["project_id"] or "")
        if not project or (requested and requested != project):
            raise PlatformOperationError("Mapped tools require the Operator's own project.")
        return project

    def entries(self):
        service = self.management
        for integration in service.integrations.list():
            if not integration["enabled"] or service._manual_kind(integration) != "mcp":
                continue
            for connection in service.connections.list(integration["id"]):
                if not connection["enabled"]:
                    continue
                for stored in service._mcp_contract_mappings(connection):
                    try:
                        mapping = service._validated_mcp_contract_mapping(integration, connection, stored)
                    except PlatformServiceError:
                        continue  # Revoked/unapproved or incompatible mappings are not tools.
                    if not mapping["enabled"]:
                        continue
                    remote = next((t for t in connection.get("metadata", {}).get("mcp", {}).get("tools", [])
                                   if t.get("name") == mapping["tool_name"]), {})
                    if not mapping["write_enabled"] and (
                        remote.get("read_only") is not True or remote.get("safety_known") is not True
                    ):
                        continue
                    identity = [integration["id"], connection["id"], connection.get("endpoint_url"),
                                connection.get("project_id"), mapping, remote]
                    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
                    yield "lynewright.integrations.mapped." + digest, integration, connection, mapping

    def operation_catalog(self, actor: Any, requested: str = "") -> list[dict[str, Any]]:
        project = self.project(actor, requested)
        capabilities = allowed_capabilities(actor)
        result = []
        for name, integration, connection, mapping in self.entries():
            if mapping["required_capability"] not in capabilities:
                continue
            if connection.get("project_id") not in {None, "", project}:
                continue
            resources = self.management.inventory.list(
                integration_id=integration["id"], connection_id=connection["id"], limit=50,
            )["items"]
            # A shared connection does not expose other projects' inventory.
            if not connection.get("project_id"):
                bound = {b["external_resource_id"] for b in self.management.bindings.list(project_id=project)
                         if b["integration_id"] == integration["id"] and b["connection_id"] == connection["id"]}
                resources = [r for r in resources if r["external_id"] in bound]
            result.append({
                "tool": name, "integration_id": integration["id"],
                "provider_name": integration.get("name", integration["id"]),
                "connection_id": connection["id"], "project_id": project,
                "contract": mapping["target_contract"], "operation": mapping["target_operation"],
                "input_paths": sorted({v for v in mapping["argument_mapping"].values() if isinstance(v, str)}),
                "requires_approval": bool(mapping["write_enabled"]),
                "resources": [{k: r[k] for k in ("external_id", "name", "resource_type", "state")}
                              for r in resources if r["state"] == "ACTIVE"],
            })
        return result

    def definitions(self) -> list[CoreToolDefinition]:
        result = []
        for name, integration, connection, mapping in self.entries():
            write = mapping["write_enabled"]
            # Unknown/non-read-only remote behavior must never be exposed as a read.
            if not write:
                remote = next((t for t in connection.get("metadata", {}).get("mcp", {}).get("tools", [])
                               if t.get("name") == mapping["tool_name"]), {})
                if remote.get("read_only") is not True or remote.get("safety_known") is not True:
                    continue
            input_paths = sorted({
                value.split(".", 1)[0]
                for value in mapping["argument_mapping"].values()
                if isinstance(value, str) and value
            })
            direct_properties = {
                path: {} for path in input_paths
                if path not in {"project_id", "idempotency_key", "arguments"}
            }
            result.append(CoreToolDefinition(
                name=name, capability=mapping["required_capability"], handler=self.handler(name),
                description=f"Configured {integration.get('name', integration['id'])} mapping: "
                            f"{mapping['target_contract']}/{mapping['target_operation']}. "
                            "Use integrations.operation_catalog for exact input paths and resource IDs. "
                            "Pass mapped input values as direct tool arguments. Writes require a unique idempotency_key.",
                read_only=not write, destructive=bool(mapping["destructive"]),
                risk_class="DESTRUCTIVE" if mapping["destructive"] else "PUBLICATION" if write else "READ_ONLY",
                source="integration",
                input_schema={"type": "object", "properties": {
                    "project_id": {"type": "string"}, **direct_properties,
                    "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
                }, "required": ["project_id", *direct_properties] + (["idempotency_key"] if write else []),
                    "additionalProperties": False},
            ))
        return result

    def handler(self, expected_name: str):
        def invoke(actor: Any, arguments: dict[str, Any]):
            project = self.project(actor, str(arguments.get("project_id") or ""))
            entry = next((entry for entry in self.entries() if entry[0] == expected_name), None)
            if entry is None:
                raise PlatformOperationError("Mapping changed or was revoked; rediscover it and request new approval.")
            _, integration, connection, mapping = entry
            require_capability(actor, mapping["required_capability"])
            if connection.get("project_id") not in {None, "", project}:
                raise PlatformOperationError("Integration connection belongs to another project.")
            # Legacy nested ``arguments`` remains accepted for interrupted
            # runs, while new tool schemas present direct fields to agents.
            source = dict(arguments.get("arguments") or {}) if isinstance(arguments.get("arguments"), dict) else {}
            source.update({
                key: value for key, value in arguments.items()
                if key not in {"project_id", "idempotency_key", "arguments"}
            })
            if arguments.get("idempotency_key"):
                source["idempotency_key"] = arguments["idempotency_key"]
            operation_id = self.operation_catalog._operation_id(integration, connection, mapping)
            resolved = self.operation_catalog.resolve_operation(
                actor, project_id=project, operation_id=operation_id,
                contract=mapping["target_contract"], operation=mapping["target_operation"],
            )
            execution = self.operation_catalog.invoke(
                actor, project_id=project, operation_id=resolved["operation_id"], arguments=source,
            )
            return execution.get("result") if isinstance(execution, dict) and "execution_id" in execution else execution
        return invoke


def build_operator_mapped_tools(store: Any, tenant_id: str, contributions: Any) -> OperatorMappedTools:
    from app.services.integration_registry import IntegrationRegistry
    from app.services.integration_connections import ConnectionProfileService
    from app.services.integration_management import IntegrationManagementService

    registry = IntegrationRegistry(store, tenant_id=tenant_id)
    return OperatorMappedTools(IntegrationManagementService(
        registry, ConnectionProfileService(store, tenant_id=tenant_id, registry=registry), contributions,
    ))
