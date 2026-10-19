from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_openapi import IntegrationOpenAPIClient
from app.services.integration_management import IntegrationManagementService
from app.services.integration_mapping_bootstrap import suggest_mapping_paths
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformValidationError, PlatformStore


class Logger:
    def write(self, level: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        return None


class SDK:
    logger = Logger()


TOOLS = [{"name": "listResources", "description": "List resources", "read_only": True, "destructive": False, "safety_known": True}]


def setup() -> tuple[IntegrationManagementService, str, sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    connections = ConnectionProfileService(store, registry=integrations, endpoint_validator=lambda _: {})
    service = IntegrationManagementService(integrations, connections, ContributionRegistry(integrations, connections=connections))
    integration_id = service.create_manual(name="MCP preview fixture", runtime_kind="mcp", endpoint_url="https://mcp.example.test")["integration"]["id"]
    service.set_authorization(integration_id, permission_mode="custom", granted_capabilities=["network:use"])
    service.connections.configure(integration_id, "primary", metadata={"mcp": {"tools": TOOLS}})
    service.approve_mcp_tools(integration_id, "primary", ["listResources"])
    return service, integration_id, db


def mapping(**overrides: object) -> dict[str, object]:
    return {
        "mapping_id": "resources", "resource_type": "fixture_resource", "label": "Fixture resource", "plural_label": "Fixture resources",
        "list_tool_name": "listResources", "list_arguments": {"organization": "org-1"}, "items_path": "payload.items", "id_path": "id", "name_path": "displayName",
        "parent_id_path": None, "url_path": "url", "status_path": "status", "display_fields": [], "metadata_paths": {}, "enabled": True, **overrides,
    }


def test_invoke_false_remains_pure_validation() -> None:
    service, integration_id, db = setup()
    expected = service._validated_mcp_resource_mapping(service.integrations.get(integration_id), service.connections.get(integration_id, "primary"), mapping())
    actual = service.validate_mcp_resource_mapping(integration_id, "primary", mapping())
    assert actual == expected
    db.close()


def test_live_preview_reuses_projection_without_inventory_or_health_write(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    sdk = SDK()
    sample = {"payload": {"items": [{"id": "r-1", "displayName": "One", "url": "https://example.test/1", "status": "ready"}]}}
    monkeypatch.setattr(service, "_sdk", lambda *_: sdk)
    monkeypatch.setattr(service.mcp, "call_tool", lambda *_args, **_kwargs: sample)
    before_inventory = service.resources(integration_id, connection_id="primary")["items"]
    before_health = service.connections.get(integration_id, "primary")["health"]
    preview = service.validate_mcp_resource_mapping(integration_id, "primary", mapping(), invoke=True)
    dry_projection = service._discover_mcp_mapping(integration_id, "primary", service.validate_mcp_resource_mapping(integration_id, "primary", mapping()), persist=False, record_health=False)
    assert preview["projected_resources"] == dry_projection["items"]
    assert preview["raw_sample"] == sample
    assert preview["suggestions"]["items_path"][0]["path"] == "payload.items"
    assert service.resources(integration_id, connection_id="primary")["items"] == before_inventory
    assert service.connections.get(integration_id, "primary")["health"] == before_health
    db.close()


def test_live_mcp_preview_is_allowed_when_integration_disabled_and_stays_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    sample = {"payload": {"items": [{"id": "r-1", "displayName": "One"}]}}
    monkeypatch.setattr(service.mcp, "call_tool", lambda *_args, **_kwargs: sample)
    before_inventory = service.resources(integration_id, connection_id="primary")["items"]
    before_health = service.connections.get(integration_id, "primary")["health"]

    preview = service.validate_mcp_resource_mapping(integration_id, "primary", mapping(), invoke=True)

    assert preview["projected_resources"][0]["external_id"] == "r-1"
    assert service.integrations.get(integration_id)["enabled"] is False
    assert service.resources(integration_id, connection_id="primary")["items"] == before_inventory
    assert service.connections.get(integration_id, "primary")["health"] == before_health
    db.close()


def test_preview_reports_precise_path_failure() -> None:
    service, integration_id, db = setup()
    service._sdk = lambda *_: SDK()  # type: ignore[method-assign]
    service.mcp.call_tool = lambda *_args, **_kwargs: {"payload": {"items": []}}  # type: ignore[method-assign]
    with pytest.raises(PlatformValidationError, match=r"items_path 'missing.items' matched nothing"):
        service.validate_mcp_resource_mapping(integration_id, "primary", mapping(items_path="missing.items"), invoke=True)
    db.close()


def test_path_suggestions_require_common_fields_and_tolerate_unrecognized_shapes() -> None:
    suggestions = suggest_mapping_paths({"outer": {"records": [{"uuid": "1", "displayName": "A"}, {"uuid": "2", "displayName": "B"}]}})
    assert suggestions["items_path"][0]["path"] == "outer.records"
    assert suggestions["id_path"][0]["path"] == "uuid"
    assert suggestions["name_path"][0]["path"] == "displayName"
    assert suggest_mapping_paths({"message": "not a resource list"}) == {"items_path": [], "id_path": [], "name_path": [], "url_path": [], "status_path": []}


def test_generic_fixture_bootstrap_probes_id_previews_saves_discovers_and_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    tools = [
        {"name": "getWorkspace", "description": "Read workspace", "read_only": True, "destructive": False, "safety_known": True},
        {"name": "listResources", "description": "List resources", "read_only": True, "destructive": False, "safety_known": True},
    ]
    monkeypatch.setattr(
        service,
        "_discover_mcp",
        lambda *_args: {"protocol_version": "fixture", "tools": tools, "resources": []},
    )
    monkeypatch.setattr(service, "_sdk", lambda *_args: SDK())

    def call_tool(_sdk: Any, *, tool_name: str, arguments: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if tool_name == "getWorkspace":
            return {"workspace": {"id": "workspace-1"}}
        assert tool_name == "listResources"
        assert arguments == {"workspaceId": "workspace-1"}
        return {"payload": {"items": [{"uuid": "resource-1", "displayName": "Fixture resource", "status": "ready"}]}}

    monkeypatch.setattr(service.mcp, "call_tool", call_tool)
    service.set_integration_enabled(integration_id, enabled=True)
    service.discover(integration_id, "primary")
    service.approve_mcp_tools(integration_id, "primary", ["getWorkspace", "listResources"])

    probe = service.probe_mcp_tool(integration_id, "primary", "getWorkspace", {})
    workspace_id = probe["result"]["workspace"]["id"]
    draft = mapping(list_tool_name="listResources", list_arguments={"workspaceId": workspace_id}, id_path="uuid", name_path="displayName", url_path=None, status_path="status")
    preview = service.validate_mcp_resource_mapping(integration_id, "primary", draft, invoke=True)
    draft["items_path"] = preview["suggestions"]["items_path"][0]["path"]
    draft["id_path"] = preview["suggestions"]["id_path"][0]["path"]
    draft["name_path"] = preview["suggestions"]["name_path"][0]["path"]
    service.set_integration_enabled(integration_id, enabled=False)
    service.create_mcp_resource_mapping(integration_id, "primary", draft)
    service.set_integration_enabled(integration_id, enabled=True)

    discovered = service.discover(integration_id, "primary")
    resource = next(item for item in discovered["resources"] if item["resource_type"] == "fixture_resource")
    assert resource["external_id"] == "resource-1"
    bound = service.bind(resource["id"], project_id="p", resource_type="fixture_resource", resource_id="canonical", role="primary", ownership_mode="linked")
    assert bound["resource_type"] == "fixture_resource"
    db.close()


def test_openapi_live_preview_is_connection_scoped_and_does_not_refresh_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    connections = ConnectionProfileService(store, registry=integrations, endpoint_validator=lambda _: {})
    service = IntegrationManagementService(integrations, connections, ContributionRegistry(integrations, connections=connections))
    integration_id = service.create_manual(
        name="OpenAPI preview fixture", runtime_kind="openapi", endpoint_url="https://openapi.example.test",
        document={"openapi": "3.0.3", "info": {"title": "Fixture", "version": "1"}, "paths": {"/resources": {"get": {"operationId": "listResources", "responses": {"200": {}}}}}},
    )["integration"]["id"]
    service.create_openapi_resource_mapping(integration_id, {"resource_type": "fixture_resource", "label": "Fixture", "plural_label": "Fixtures", "list_operation_id": "listResources", "items_path": "data.items", "id_path": "id", "name_path": "name", "display_fields": [], "metadata_paths": {}, "enabled": True})
    service.approve_openapi_operations(integration_id, "primary", ["listResources"])
    service.set_integration_enabled(integration_id, enabled=True)
    monkeypatch.setattr(service, "execute_openapi", lambda *_args, **_kwargs: {"data": {"items": [{"id": "one", "name": "One"}]}})
    before_inventory = service.resources(integration_id, connection_id="primary")["items"]
    before_health = service.connections.get(integration_id, "primary")["health"]
    preview = service.validate_openapi_resource_mapping(integration_id, {"resource_type": "fixture_resource", "label": "Fixture", "plural_label": "Fixtures", "list_operation_id": "listResources", "items_path": "data.items", "id_path": "id", "name_path": "name", "display_fields": [], "metadata_paths": {}, "enabled": True}, connection_id="primary", invoke=True)
    assert preview["projected_resources"][0]["external_id"] == "one"
    assert preview["suggestions"]["items_path"][0]["path"] == "data.items"
    assert service.resources(integration_id, connection_id="primary")["items"] == before_inventory
    assert service.connections.get(integration_id, "primary")["health"] == before_health
    db.close()


def test_live_openapi_preview_is_allowed_when_integration_disabled_and_stays_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    connections = ConnectionProfileService(store, registry=integrations, endpoint_validator=lambda _: {})
    service = IntegrationManagementService(integrations, connections, ContributionRegistry(integrations, connections=connections))
    integration_id = service.create_manual(
        name="Disabled OpenAPI preview fixture", runtime_kind="openapi", endpoint_url="https://openapi.example.test",
        document={"openapi": "3.0.3", "info": {"title": "Fixture", "version": "1"}, "paths": {"/resources": {"get": {"operationId": "listResources", "responses": {"200": {}}}}}},
    )["integration"]["id"]
    service.approve_openapi_operations(integration_id, "primary", ["listResources"])
    sample = {"data": {"items": [{"id": "one", "name": "One"}]}}
    monkeypatch.setattr(IntegrationOpenAPIClient, "execute", lambda *_args, **_kwargs: sample)
    draft = {
        "resource_type": "fixture_resource", "label": "Fixture", "plural_label": "Fixtures",
        "list_operation_id": "listResources", "items_path": "data.items", "id_path": "id",
        "name_path": "name", "display_fields": [], "metadata_paths": {}, "enabled": True,
    }
    before_inventory = service.resources(integration_id, connection_id="primary")["items"]
    before_health = service.connections.get(integration_id, "primary")["health"]

    preview = service.validate_openapi_resource_mapping(
        integration_id, draft, connection_id="primary", invoke=True,
    )

    assert preview["projected_resources"][0]["external_id"] == "one"
    assert service.integrations.get(integration_id)["enabled"] is False
    assert service.resources(integration_id, connection_id="primary")["items"] == before_inventory
    assert service.connections.get(integration_id, "primary")["health"] == before_health
    db.close()
