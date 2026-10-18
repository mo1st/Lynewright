"""Operator-facing projections of the provider-neutral integration host.

The Operator does not call a provider by name.
It calls a stable Lynewright contract, while selection, capability evidence,
request-scoped SDK construction, and tenant checks remain owned by the
integration host. Mutation tools are deliberately ``PUBLICATION`` (or
``DESTRUCTIVE``) so the Operator loop pauses and the Telegram transport can
ask the human for approval before the provider is reached.
"""
from __future__ import annotations

from typing import Any

from app.services.capability_context import CapabilityInvocationContext
from app.services.platform_services import PlatformOperationError, PlatformValidationError
from app.services.tool_models import CoreToolDefinition


_SOCIAL_CONTRACT = "lynewright.social.publisher"
_VIDEO_CONTRACT = "lynewright.video.render"
_INTEGRATION_ENTRY_POINT = "operator.telegram.integration"


def _provider_business_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return only declared business fields for a direct service contract.

    Scope and execution metadata belong to the host invocation context.  In
    particular, ``idempotency_key`` is pinned and audited by Lynewright but must
    reach a provider method only when that provider explicitly declares it in
    its operation schema.  The generic operation catalog already performs the
    same separation for prepared invocations; compatibility aliases use this
    helper so they cannot leak host fields into strict future providers.
    """
    return {
        key: value for key, value in arguments.items()
        if key not in {"project_id", "integration_id", "connection_id", "idempotency_key"}
    }


def _project_id(arguments: dict[str, Any]) -> str:
    project_id = str(arguments.get("project_id") or "").strip()
    if not project_id:
        raise PlatformValidationError("Integration actions require a project_id.")
    return project_id


def _provider_scope(
    registry: Any,
    *,
    contract: str,
    project_id: str,
    integration_id: str | None,
    connection_id: str | None,
) -> tuple[str, str | None]:
    provider = registry.resolve(
        contract,
        project_id=project_id,
        provider_integration_id=integration_id or None,
    )
    resolved_integration = str(provider.integration_id)
    resolved_connection = str(connection_id or provider.connection_id or "") or None
    return resolved_integration, resolved_connection


def _invoke_contract(
    *,
    actor: Any,
    tenant_id: str,
    arguments: dict[str, Any],
    contract: str,
    operation: str,
    payload: dict[str, Any],
    host_available: bool,
    # The Operator's durable human approval is the mutation gate.  A compiled
    # active provider may still carry DECLARED evidence until its optional
    # transport probe runs; blocking approved calls on that probe state
    # made a newly installed, sole provider appear unavailable.  Callers that
    # require a live probe can still pass require_verified=True explicitly.
    require_verified: bool = False,
) -> dict[str, Any]:
    if not host_available:
        raise PlatformOperationError("The integration host is not available for this tenant.")
    from app.services.capability_dispatch import get_contract_registry

    project_id = _project_id(arguments)
    integration_id = str(arguments.get("integration_id") or "").strip() or None
    connection_id = str(arguments.get("connection_id") or "").strip() or None
    registry = get_contract_registry(tenant_id)
    resolved_integration, resolved_connection = _provider_scope(
        registry,
        contract=contract,
        project_id=project_id,
        integration_id=integration_id,
        connection_id=connection_id,
    )
    context = CapabilityInvocationContext.from_actor(
        actor,
        tenant_id=tenant_id,
        actor_type="operator",
        entry_point=_INTEGRATION_ENTRY_POINT,
        project_id=project_id,
        integration_id=resolved_integration,
        connection_id=resolved_connection,
        idempotency_key=(str(arguments.get("idempotency_key") or "").strip() or None),
    )
    return registry.invoke(
        contract,
        operation,
        payload,
        project_id=project_id,
        require_verified=require_verified,
        actor=actor,
        invocation_context=context,
        provider_integration_id=resolved_integration,
    )


def _social_handler(tenant_id: str, operation: str, *, host_available: bool):
    def handler(actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = _project_id(arguments)
        payload = _provider_business_payload(arguments)
        if operation in {"posts.create", "posts.schedule"} and not str(payload.get("content") or "").strip():
            raise PlatformValidationError("A social post requires content.")
        if operation == "posts.schedule" and not str(payload.get("scheduled_for") or "").strip():
            raise PlatformValidationError("A scheduled post requires scheduled_for.")
        if operation == "posts.cancel" and not str(payload.get("post_id") or "").strip():
            raise PlatformValidationError("Cancelling a social post requires post_id.")
        return _invoke_contract(
            actor=actor, tenant_id=tenant_id, arguments={**arguments, "project_id": project_id},
            contract=_SOCIAL_CONTRACT, operation=operation, payload=payload,
            host_available=host_available,
        )

    return handler


def _video_handler(tenant_id: str, operation: str, *, host_available: bool):
    def handler(actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = _provider_business_payload(arguments)
        if operation == "media.video.status" and not str(payload.get("generation_id") or "").strip():
            raise PlatformValidationError("Video status requires generation_id.")
        return _invoke_contract(
            actor=actor, tenant_id=tenant_id, arguments=arguments,
            contract=_VIDEO_CONTRACT, operation=operation, payload=payload,
            host_available=host_available,
        )

    return handler


def _catalog_handler(contributions: Any | None, tenant_id: str, mapped: Any | None = None):
    def handler(_actor: Any, _arguments: dict[str, Any]) -> dict[str, Any]:
        if contributions is None:
            return {"tenant_id": tenant_id, "services": [], "mcp_tools": []}
        services = [
            {
                "id": item.id,
                "integration_id": item.integration_id,
                "contract": str(item.metadata.get("contract") or ""),
                "version": str(item.metadata.get("version") or ""),
                "capabilities": list(item.metadata.get("capabilities") or []),
                "invocation": str(item.metadata.get("invocation") or "direct"),
            }
            for item in contributions.list(kind="service")
        ]
        mcp_tools = [
            {
                "id": item.id,
                "integration_id": item.integration_id,
                "name": str(item.metadata.get("name") or ""),
                "description": str(item.metadata.get("description") or ""),
                "caller_capability": str(item.metadata.get("caller_capability") or ""),
                "integration_capability": str(item.metadata.get("integration_capability") or ""),
                "project_scope": str(item.metadata.get("project_scope") or ""),
                "risk_class": str(item.metadata.get("risk_class") or "PUBLICATION"),
                "read_only": bool(item.metadata.get("read_only", False)),
            }
            for item in contributions.list(kind="mcp")
        ]
        mappings = mapped.operation_catalog(_actor, str(_arguments.get("project_id") or "")) if mapped else []
        return {"tenant_id": tenant_id, "services": services, "mcp_tools": mcp_tools, "mapped_tools": mappings}

    return handler


def _operation_search_handler(operation_catalog: Any | None):
    def handler(actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation_catalog is None:
            return {"operations": []}
        project_id = _project_id(arguments)
        return {"operations": operation_catalog.search(
            actor, project_id=project_id, query=str(arguments.get("query") or ""),
            limit=int(arguments.get("limit") or 12),
            compact=bool(arguments.get("compact", True)),
            include_schema=bool(arguments.get("include_schema", False)),
        )}

    return handler


def _operation_describe_handler(operation_catalog: Any | None):
    def handler(actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation_catalog is None:
            raise PlatformOperationError("The integration operation catalog is not available for this tenant.")
        return operation_catalog.describe(
            actor,
            project_id=_project_id(arguments),
            operation_id=str(arguments.get("operation_id") or ""),
        )

    return handler


def _operation_resources_handler(operation_catalog: Any | None):
    def handler(actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation_catalog is None:
            raise PlatformOperationError("The integration operation catalog is not available for this tenant.")
        return operation_catalog.search_resources(
            actor, project_id=_project_id(arguments),
            query=str(arguments.get("query") or ""),
            resource_type=str(arguments.get("resource_type") or ""),
            integration_id=str(arguments.get("integration_id") or ""),
            operation_id=str(arguments.get("operation_id") or ""),
            limit=int(arguments.get("limit") or 20),
            auto_discover=bool(arguments.get("auto_discover")),
        )
    return handler


def _operation_requirements_handler(operation_catalog: Any | None):
    def handler(actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation_catalog is None:
            raise PlatformOperationError("The integration operation catalog is not available for this tenant.")
        raw = arguments.get("arguments")
        if raw is not None and not isinstance(raw, dict):
            raise PlatformValidationError("Integration requirement arguments must be an object.")
        return operation_catalog.resolve_requirements(
            actor, project_id=_project_id(arguments),
            operation_id=str(arguments.get("operation_id") or ""),
            arguments=raw or {}, auto_discover=bool(arguments.get("auto_discover", True)),
        )
    return handler


def _operation_prepare_handler(operation_catalog: Any | None):
    def handler(actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation_catalog is None:
            raise PlatformOperationError("The integration operation catalog is not available for this tenant.")
        raw = arguments.get("arguments")
        if not isinstance(raw, dict):
            raise PlatformValidationError("Integration operation arguments must be an object.")
        return operation_catalog.prepare(
            actor,
            project_id=_project_id(arguments),
            operation_id=str(arguments.get("operation_id") or ""),
            arguments=raw,
        )

    return handler


def _operation_invoke_handler(operation_catalog: Any | None):
    def handler(actor: Any, arguments: dict[str, Any]) -> Any:
        if operation_catalog is None:
            raise PlatformOperationError("The integration operation catalog is not available for this tenant.")
        raw = arguments.get("arguments")
        prepared_id = str(arguments.get("prepared_invocation_id") or "").strip()
        if not isinstance(raw, dict) and not prepared_id:
            raise PlatformValidationError("Integration operation arguments must be an object.")
        project_id = _project_id(arguments)
        payload = dict(raw or {})
        payload.setdefault("project_id", project_id)
        # The outer generic invocation key is copied into the mapped payload so
        # every transport uses the same provider-write idempotency contract.
        if str(arguments.get("idempotency_key") or "").strip():
            payload.setdefault("idempotency_key", str(arguments["idempotency_key"]).strip())
        return operation_catalog.invoke(
            actor, project_id=project_id,
            operation_id=str(arguments.get("operation_id") or ""),
            arguments=payload,
            prepared_invocation_id=prepared_id or None,
        )

    return handler


def _operation_get_execution_handler(operation_catalog: Any | None):
    def handler(actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation_catalog is None:
            raise PlatformOperationError("The integration operation catalog is not available for this tenant.")
        return operation_catalog.get_execution(
            actor, project_id=_project_id(arguments),
            execution_id=str(arguments.get("execution_id") or ""),
        )
    return handler


def _operation_cancel_execution_handler(operation_catalog: Any | None):
    def handler(actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation_catalog is None:
            raise PlatformOperationError("The integration operation catalog is not available for this tenant.")
        return operation_catalog.cancel_execution(
            actor, project_id=_project_id(arguments),
            execution_id=str(arguments.get("execution_id") or ""),
        )
    return handler


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "integration_id": {"type": "string"},
            "connection_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
            **properties,
        },
        "required": list(required or ["project_id"]),
        "additionalProperties": True,
    }


def register_operator_integration_tools(
    runtime: Any, *, tenant_id: str, contributions: Any | None = None,
) -> list[str]:
    """Register idempotent, approval-gated integration tools on one runtime."""

    existing = {definition.name for definition in runtime.definitions()}
    host_available = contributions is not None
    mapped = None
    if host_available:
        from app.services.operator_mapped_tools import build_operator_mapped_tools
        mapped = build_operator_mapped_tools(runtime.store, tenant_id, contributions)
    operation_catalog = None
    if mapped is not None:
        from app.services.integration_operations import IntegrationOperationCatalog
        operation_catalog = IntegrationOperationCatalog(mapped, native_runtime=runtime)
    definitions = [
        CoreToolDefinition(
            name="lynewright.integrations.operation_catalog",
            capability="system:read",
            handler=_catalog_handler(contributions, tenant_id, mapped),
            description="Discover current provider contracts, configured MCP mappings, exact invocation tools and resource IDs.",
            read_only=True,
            risk_class="READ_ONLY",
            input_schema={"type": "object", "properties": {"project_id": {"type": "string"}}, "additionalProperties": False},
        ),
        # Stable, provider-neutral operation surface. Existing category tools
        # below remain compatibility aliases during the migration window.
        CoreToolDefinition(
            name="lynewright.integrations.search_operations",
            capability="system:read",
            handler=_operation_search_handler(operation_catalog),
            description="Search eligible integration operations by their declared metadata and schema.",
            read_only=True,
            risk_class="READ_ONLY",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"}, "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "compact": {"type": "boolean"}, "include_schema": {"type": "boolean"},
                },
                "required": ["project_id"], "additionalProperties": False,
            },
        ),
        CoreToolDefinition(
            name="lynewright.integrations.describe_operation",
            capability="system:read",
            handler=_operation_describe_handler(operation_catalog),
            description="Describe one eligible integration operation, including its schema, effect, and revision.",
            read_only=True,
            risk_class="READ_ONLY",
            input_schema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}, "operation_id": {"type": "string"}},
                "required": ["project_id", "operation_id"], "additionalProperties": False,
            },
        ),
        CoreToolDefinition(
            name="lynewright.integrations.search_resources",
            capability="system:read",
            handler=_operation_resources_handler(operation_catalog),
            description="Search the project-scoped external-resource inventory for any configured integration before supplying required IDs.",
            read_only=True,
            risk_class="READ_ONLY",
            input_schema={
                "type": "object", "properties": {
                    "project_id": {"type": "string"}, "query": {"type": "string"},
                    "resource_type": {"type": "string"}, "integration_id": {"type": "string"},
                    "operation_id": {"type": "string"}, "limit": {"type": "integer"},
                    "auto_discover": {"type": "boolean"},
                }, "required": ["project_id"], "additionalProperties": False,
            },
        ),
        CoreToolDefinition(
            name="lynewright.integrations.resolve_requirements",
            capability="system:read",
            handler=_operation_requirements_handler(operation_catalog),
            description="Resolve generic integration operation requirements and return safe arguments patches or explicit blockers before approval.",
            read_only=True,
            risk_class="READ_ONLY",
            input_schema={
                "type": "object", "properties": {
                    "project_id": {"type": "string"}, "operation_id": {"type": "string"},
                    "arguments": {"type": "object"}, "auto_discover": {"type": "boolean"},
                }, "required": ["project_id", "operation_id"], "additionalProperties": False,
            },
        ),
        CoreToolDefinition(
            name="lynewright.integrations.prepare_invocation",
            capability="mcp:client:use",
            handler=_operation_prepare_handler(operation_catalog),
            description="Validate and canonicalize an integration operation before execution or approval.",
            read_only=True,
            risk_class="SAFE_OPERATION",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"}, "operation_id": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["project_id", "operation_id", "arguments"], "additionalProperties": False,
            },
        ),
        CoreToolDefinition(
            name="lynewright.integrations.invoke",
            capability="mcp:client:use",
            handler=_operation_invoke_handler(operation_catalog),
            description="Invoke a prepared-by-schema integration operation through its configured transport.",
            risk_class="PUBLICATION",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"}, "operation_id": {"type": "string"},
                    "arguments": {"type": "object"}, "prepared_invocation_id": {"type": "string"},
                    "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
                },
                "required": ["project_id", "operation_id"],
                "additionalProperties": False,
            },
        ),
        CoreToolDefinition(
            name="lynewright.integrations.get_execution",
            capability="system:read",
            handler=_operation_get_execution_handler(operation_catalog),
            description="Read the durable state and sanitized result of a generic integration execution.",
            read_only=True,
            risk_class="READ_ONLY",
            input_schema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}, "execution_id": {"type": "string"}},
                "required": ["project_id", "execution_id"], "additionalProperties": False,
            },
        ),
        CoreToolDefinition(
            name="lynewright.integrations.cancel_execution",
            capability="mcp:client:use",
            handler=_operation_cancel_execution_handler(operation_catalog),
            description="Request cancellation of a running generic integration execution when supported.",
            destructive=True,
            risk_class="DESTRUCTIVE",
            reports_terminal_state=False,
            input_schema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}, "execution_id": {"type": "string"}},
                "required": ["project_id", "execution_id"], "additionalProperties": False,
            },
        ),
        CoreToolDefinition(
            name="lynewright.social.publish.create",
            capability="publishing:write",
            handler=_social_handler(tenant_id, "posts.create", host_available=host_available),
            description="Create a social post through the selected provider, or the only active compatible provider.",
            risk_class="PUBLICATION",
            input_schema=_schema({
                "account_id": {"type": "string"},
                "content": {"type": "string"},
                "media_ids": {"type": "array", "items": {"type": "string"}},
            }, ["project_id", "account_id", "content"]),
        ),
        CoreToolDefinition(
            name="lynewright.social.publish.schedule",
            capability="publishing:write",
            handler=_social_handler(tenant_id, "posts.schedule", host_available=host_available),
            description="Schedule a social post through the selected provider, or the only active compatible provider.",
            risk_class="PUBLICATION",
            input_schema=_schema({
                "account_id": {"type": "string"},
                "content": {"type": "string"},
                "scheduled_for": {"type": "string"},
                "media_ids": {"type": "array", "items": {"type": "string"}},
            }, ["project_id", "account_id", "content", "scheduled_for"]),
        ),
        CoreToolDefinition(
            name="lynewright.social.publish.cancel",
            capability="publishing:write",
            handler=_social_handler(tenant_id, "posts.cancel", host_available=host_available),
            description="Cancel a social post through the selected provider, or the only active compatible provider.",
            risk_class="DESTRUCTIVE",
            reports_terminal_state=False,
            input_schema=_schema({"post_id": {"type": "string"}}, ["project_id", "post_id"]),
        ),
        CoreToolDefinition(
            name="lynewright.media.video.render",
            capability="media:create",
            handler=_video_handler(tenant_id, "media.video.render", host_available=host_available),
            description="Start a bounded video render through the selected provider, or the only active compatible provider.",
            risk_class="PUBLICATION",
            input_schema=_schema({
                "subject": {"type": "string"}, "script": {"type": "string"},
                "terms": {"type": "array", "items": {"type": "string"}},
                # Provider-defined. Omit unless a supported value was
                # discovered; the selected integration applies its own safe
                # default rather than asking the model to invent a vendor ID.
                "voice_name": {"type": "string", "description": "Optional provider-defined voice identifier."},
                "material": {"type": "string"},
                "subtitle_enabled": {"type": "boolean"}, "clip_duration": {"type": "number"},
                "video_aspect": {"type": "string"}, "video_source": {"type": "string"},
            }, ["project_id", "script", "terms"]),
        ),
        CoreToolDefinition(
            name="lynewright.media.video.status",
            capability="media:read",
            handler=_video_handler(tenant_id, "media.video.status", host_available=host_available),
            description="Read the status of a provider video render through the selected provider, or the only active compatible provider.",
            read_only=True,
            risk_class="READ_ONLY",
            input_schema=_schema({"generation_id": {"type": "string"}}, ["project_id", "generation_id"]),
        ),
    ]
    registered: list[str] = []
    if mapped is not None:
        definitions.extend(mapped.definitions())
    for definition in definitions:
        if definition.name in existing:
            continue
        runtime.register_tool(definition)
        registered.append(definition.name)
    return registered


__all__ = ["register_operator_integration_tools"]
