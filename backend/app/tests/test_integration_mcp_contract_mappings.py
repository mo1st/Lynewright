from __future__ import annotations

import sqlite3

import pytest

from app.services.control_plane_registry import CapabilityDescriptor, ControlPlaneRegistry
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_management import IntegrationManagementService
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformOperationError, PlatformStore, PlatformValidationError


def setup() -> tuple[IntegrationManagementService, str, sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    integration_id = "lynewright.manual.mcp-contract-fixture"
    integrations.register({
        "schema_version": "1.0", "id": integration_id, "name": "MCP contract fixture", "version": "1.0.0", "type": "service",
        "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0", "requested_capabilities": ["network:use"],
        "service": {"kind": "http", "health_check": "/health"}, "backend": "manual-runtime",
    }, configuration={"manual": {"runtime": "mcp"}})
    integrations.set_authorization(integration_id, permission_mode="custom", granted_capabilities=["network:use"])
    integrations.enable(integration_id)
    connections = ConnectionProfileService(store, registry=integrations, endpoint_validator=lambda _: {})
    connections.create(
        integration_id, connection_id="primary", name="Primary", endpoint_url="https://mcp.example.test",
        configuration={"mcp": {"approved_tools": ["lookup"]}},
        metadata={"mcp": {"tools": [{"name": "lookup", "description": "Safe lookup"}]}},
    )
    plane = ControlPlaneRegistry()
    plane.capabilities.register(CapabilityDescriptor(
        id="fixture.read", version="1.0.0", contract_id="lynewright.fixture.read", operation="fetch",
        name="Fixture read", description="Controlled read contract", required_actor_capability="network:use",
        risk_class="LOW_RISK", project_scope="none", resource_scope="none", connection_scope="connection",
        output_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
    ))
    return IntegrationManagementService(integrations, connections, ContributionRegistry(integrations, connections=connections), control_plane=plane), integration_id, db


def mapping(**overrides: object) -> dict[str, object]:
    return {
        "mapping_id": "safe-read", "tool_name": "lookup", "target_contract": "lynewright.fixture.read", "target_operation": "fetch",
        "argument_mapping": {"query": "query"}, "output_mapping": {"value": "answer"}, "enabled": True,
        **overrides,
    }


def test_mapping_requires_discovered_approved_tool_existing_contract_and_grant() -> None:
    service, integration_id, db = setup()
    connection = service.connections.get(integration_id, "primary")
    service.connections.configure(integration_id, "primary", configuration={"mcp": {"approved_tools": []}})
    with pytest.raises(PlatformOperationError, match="approved"):
        service.create_mcp_contract_mapping(integration_id, "primary", mapping())
    service.connections.configure(integration_id, "primary", configuration=connection["configuration"])
    with pytest.raises(PlatformValidationError, match="does not exist"):
        service.create_mcp_contract_mapping(integration_id, "primary", mapping(target_contract="lynewright.unknown"))
    with pytest.raises(PlatformValidationError, match="does not exist"):
        service.create_mcp_contract_mapping(integration_id, "primary", mapping(target_operation="unknown"))
    before = service.integrations.get(integration_id)["granted_capabilities"]
    created = service.create_mcp_contract_mapping(integration_id, "primary", mapping())
    assert created["mapping_id"] == "safe-read"
    assert service.integrations.get(integration_id)["granted_capabilities"] == before
    assert service.contributions.list(kind="mcp") == []  # external discovery never re-exposes a public MCP tool
    db.close()


def test_contract_mapping_can_be_revised_or_removed_without_creating_a_duplicate() -> None:
    service, integration_id, db = setup()
    service.create_mcp_contract_mapping(integration_id, "primary", mapping())
    updated = service.update_mcp_contract_mapping(
        integration_id,
        "primary",
        "safe-read",
        mapping(argument_mapping={"query": "replacement_query"}, output_mapping={"value": "replacement_answer"}),
    )
    assert updated["mapping_id"] == "safe-read"
    assert updated["argument_mapping"] == {"query": "replacement_query"}
    assert service.mcp_contract_mappings(integration_id, "primary") == [updated]
    service.delete_mcp_contract_mapping(integration_id, "primary", "safe-read")
    assert service.mcp_contract_mappings(integration_id, "primary") == []
    with pytest.raises(PlatformNotFoundError, match="not found"):
        service.delete_mcp_contract_mapping(integration_id, "primary", "safe-read")
    db.close()


def test_mapping_is_connection_scoped_bounded_and_enforces_contract_output(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    service.create_mcp_contract_mapping(integration_id, "primary", mapping())
    service.connections.create(
        integration_id, connection_id="secondary", name="Secondary", endpoint_url="https://mcp-2.example.test",
        configuration={"mcp": {"approved_tools": ["lookup"]}}, metadata={"mcp": {"tools": [{"name": "lookup"}]}},
    )
    assert service.mcp_contract_mappings(integration_id, "secondary") == []
    with pytest.raises(PlatformValidationError, match="target is invalid"):
        service.create_mcp_contract_mapping(integration_id, "secondary", mapping(mapping_id="other", argument_mapping={"bad target!": "query"}))
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(service.mcp, "call_tool", lambda _sdk, **kwargs: calls.append(kwargs) or {"answer": "ok"})
    result = service.execute_mcp_contract_mapping(integration_id, "primary", "safe-read", {"query": "needle"})
    assert result == {"target_contract": "lynewright.fixture.read", "target_operation": "fetch", "output": {"value": "ok"}}
    assert calls[0]["arguments"] == {"query": "needle"}
    monkeypatch.setattr(service.mcp, "call_tool", lambda _sdk, **_kwargs: {"answer": 3})
    with pytest.raises(PlatformOperationError, match="incompatible"):
        service.execute_mcp_contract_mapping(integration_id, "primary", "safe-read", {"query": "needle"})
    assert service.connections.get(integration_id, "primary")["health"]["state"] == "DEGRADED"
    db.close()


def test_registered_social_contract_targets_are_exposed_to_mcp_mapping() -> None:
    service, integration_id, db = setup()
    from app.services.social_publisher_contract import SOCIAL_PUBLISHER_POST_OUTPUT_SCHEMA

    for index, operation in enumerate(("posts.cancel", "posts.create", "posts.schedule")):
        service.control_plane.capabilities.register(CapabilityDescriptor(
            id=f"fixture.social.publish.{index}", version="1.0.0",
            contract_id="lynewright.social.publisher", operation=operation,
            name=operation, description="Fixture operation for generic contract mapping",
            required_actor_capability="publishing:write",
            risk_class="DESTRUCTIVE" if operation == "posts.cancel" else "SAFE_OPERATION",
            project_scope="required", resource_scope="required", connection_scope="connection",
            output_schema=SOCIAL_PUBLISHER_POST_OUTPUT_SCHEMA,
        ))

    targets = service.mcp_contract_targets()
    social_targets = [
        (item["contract"], item["operation"])
        for item in targets
        if item["contract"] == "lynewright.social.publisher"
    ]
    assert social_targets == [
        ("lynewright.social.publisher", "posts.cancel"),
        ("lynewright.social.publisher", "posts.create"),
        ("lynewright.social.publisher", "posts.schedule"),
    ]
    assert len(social_targets) == 3
    assert all(isinstance(item["output_schema"], dict) for item in targets)
    assert not any(item["operation"] in {"posts.get", "posts.status"} for item in targets)
    db.close()


def _write_service(*, destructive: bool = False) -> tuple[IntegrationManagementService, str, sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    integration_id = "lynewright.manual.mcp-write-fixture"
    integrations.register({
        "schema_version": "1.0", "id": integration_id, "name": "MCP write fixture", "version": "1.0.0", "type": "service",
        "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0", "requested_capabilities": ["network:use", "publishing:write"],
        "service": {"kind": "http", "health_check": "/health"}, "backend": "manual-runtime",
    }, configuration={"manual": {"runtime": "mcp"}})
    integrations.set_authorization(integration_id, permission_mode="custom", granted_capabilities=["network:use", "publishing:write"])
    connections = ConnectionProfileService(store, registry=integrations, endpoint_validator=lambda _: {})
    connections.create(
        integration_id, connection_id="primary", name="Primary", endpoint_url="https://mcp.example.test",
        configuration={"mcp": {"approved_tools": ["write_tool"]}},
        metadata={"mcp": {"tools": [{
            "name": "write_tool", "description": "Fixture write", "read_only": False,
            "destructive": destructive, "safety_known": True,
        }]}},
    )
    plane = ControlPlaneRegistry()
    plane.capabilities.register(CapabilityDescriptor(
        id="fixture.write", version="1.0.0", contract_id="lynewright.fixture.write", operation="create",
        name="Fixture write", description="Controlled write contract", required_actor_capability="publishing:write",
        risk_class="DESTRUCTIVE" if destructive else "SAFE_OPERATION", project_scope="none", resource_scope="none",
        connection_scope="connection", output_schema={
            "type": "object", "properties": {"post": {
                "type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
                "required": ["id", "status"], "additionalProperties": True,
            }}, "required": ["post"], "additionalProperties": True,
        },
    ))
    service = IntegrationManagementService(
        integrations, connections, ContributionRegistry(integrations, connections=connections), control_plane=plane,
    )
    return service, integration_id, db


def _write_mapping(**overrides: object) -> dict[str, object]:
    return {
        "mapping_id": "write-contract", "tool_name": "write_tool", "target_contract": "lynewright.fixture.write",
        "target_operation": "create", "argument_mapping": {"text": "content", "schedulingType": {"$literal": "direct"}},
        "output_mapping": {"post.id": "id", "post.status": "status"}, "enabled": True, "write_enabled": True,
        **overrides,
    }


def test_mcp_write_mapping_requires_opt_in_known_write_tool_and_write_capability() -> None:
    service, integration_id, db = _write_service()
    with pytest.raises(PlatformOperationError, match="explicit write opt-in"):
        service.create_mcp_contract_mapping(integration_id, "primary", _write_mapping(write_enabled=False))

    service.control_plane.capabilities.register(CapabilityDescriptor(
        id="fixture.read-two", version="1.0.0", contract_id="lynewright.fixture.read-two", operation="fetch",
        name="Read", description="Read", required_actor_capability="network:use", risk_class="LOW_RISK",
        project_scope="none", resource_scope="none", connection_scope="connection", output_schema={"type": "object"},
    ))
    with pytest.raises(PlatformOperationError, match="write capability"):
        service.create_mcp_contract_mapping(
            integration_id, "primary", _write_mapping(mapping_id="read-target", target_contract="lynewright.fixture.read-two", target_operation="fetch"),
        )

    connection = service.connections.get(integration_id, "primary")
    service.connections.configure(
        integration_id, "primary", metadata={"mcp": {"tools": [{"name": "write_tool", "read_only": False, "destructive": False}]}},
    )
    with pytest.raises(PlatformOperationError, match="safety metadata"):
        service.create_mcp_contract_mapping(integration_id, "primary", _write_mapping(mapping_id="unknown-safety"))
    service.connections.configure(integration_id, "primary", metadata=connection["metadata"])

    created = service.create_mcp_contract_mapping(integration_id, "primary", _write_mapping())
    assert created["write_enabled"] is True
    assert created["required_capability"] == "publishing:write"
    assert created["risk_class"] == "SAFE_OPERATION"
    assert created["destructive"] is False
    db.close()


def test_mcp_write_mapping_is_idempotent_records_provenance_and_never_retries_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = _write_service()
    service.create_mcp_contract_mapping(integration_id, "primary", _write_mapping())
    service.integrations.enable(integration_id)
    with pytest.raises(PlatformValidationError, match="idempotency key"):
        service.execute_mcp_contract_mapping(integration_id, "primary", "write-contract", {"content": "hello"})

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(service.mcp, "call_tool", lambda _sdk, **kwargs: calls.append(kwargs) or {"id": "post-1", "status": "sent"})
    first = service.execute_mcp_contract_mapping(
        integration_id, "primary", "write-contract", {"content": "hello"}, idempotency_key="fixture-write-001",
    )
    second = service.execute_mcp_contract_mapping(
        integration_id, "primary", "write-contract", {"content": "hello"}, idempotency_key="fixture-write-001",
    )
    assert first == second
    assert len(calls) == 1
    assert first["provenance"] == {
        "integration_id": integration_id, "connection_id": "primary", "remote_operation_id": "write_tool",
    }
    assert first["output"] == {"post": {"id": "post-1", "status": "sent"}}
    assert calls[0]["arguments"] == {"text": "hello", "schedulingType": "direct"}
    with pytest.raises(PlatformConflictError, match="different contract action"):
        service.execute_mcp_contract_mapping(
            integration_id, "primary", "write-contract", {"content": "different"}, idempotency_key="fixture-write-001",
        )

    failures = 0
    def fail_once(*_args: object, **_kwargs: object) -> object:
        nonlocal failures
        failures += 1
        raise PlatformOperationError("remote write outcome unknown")
    monkeypatch.setattr(service.mcp, "call_tool", fail_once)
    with pytest.raises(PlatformOperationError, match="unknown"):
        service.execute_mcp_contract_mapping(
            integration_id, "primary", "write-contract", {"content": "failure"}, idempotency_key="fixture-write-002",
        )
    with pytest.raises(PlatformConflictError, match="failed"):
        service.execute_mcp_contract_mapping(
            integration_id, "primary", "write-contract", {"content": "failure"}, idempotency_key="fixture-write-002",
        )
    assert failures == 1
    db.close()


def test_destructive_mcp_contract_mapping_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = _write_service(destructive=True)
    service.create_mcp_contract_mapping(integration_id, "primary", _write_mapping())
    service.integrations.enable(integration_id)
    with pytest.raises(PlatformValidationError, match="explicit confirmation"):
        service.execute_mcp_contract_mapping(
            integration_id, "primary", "write-contract", {"content": "delete"}, idempotency_key="fixture-delete-001",
        )
    monkeypatch.setattr(service.mcp, "call_tool", lambda *_args, **_kwargs: {"id": "post-1", "status": "deleted"})
    result = service.execute_mcp_contract_mapping(
        integration_id, "primary", "write-contract", {"content": "delete"},
        idempotency_key="fixture-delete-001", confirm_destructive=True,
    )
    assert result["destructive"] is True
    assert result["output"]["post"]["status"] == "deleted"
    db.close()
