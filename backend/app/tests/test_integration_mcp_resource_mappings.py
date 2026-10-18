from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_management import IntegrationManagementService
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformConflictError, PlatformOperationError, PlatformStore, PlatformValidationError


TOOLS = [
    {"name": "listChannels", "description": "List channels"},
    {"name": "getChannel", "description": "Read one channel"},
    {"name": "createChannel", "description": "Create one channel"},
    {"name": "updateChannel", "description": "Update one channel"},
    {"name": "deleteChannel", "description": "Delete one channel"},
    {"name": "unapprovedTool", "description": "Not approved"},
]


def setup() -> tuple[IntegrationManagementService, str, sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    connections = ConnectionProfileService(store, registry=integrations, endpoint_validator=lambda _: {})
    service = IntegrationManagementService(
        integrations, connections, ContributionRegistry(integrations, connections=connections),
    )
    integration_id = service.create_manual(
        name="MCP resource fixture", runtime_kind="mcp", endpoint_url="https://mcp.example.test",
    )["integration"]["id"]
    service.set_authorization(integration_id, permission_mode="custom", granted_capabilities=["network:use"])
    service.connections.configure(
        integration_id,
        "primary",
        metadata={"mcp": {"tools": TOOLS}},
    )
    return service, integration_id, db


def mapping(**overrides: object) -> dict[str, object]:
    return {
        "mapping_id": "channels",
        "resource_type": "social_channel",
        "label": "Social channel",
        "plural_label": "Social channels",
        "list_tool_name": "listChannels",
        "list_arguments": {"organization": "org-1"},
        "get_tool_name": "getChannel",
        "create_tool_name": "createChannel",
        "update_tool_name": "updateChannel",
        "delete_tool_name": "deleteChannel",
        "get_external_id_target": "channel_id",
        "update_external_id_target": "channel_id",
        "delete_external_id_target": "channel_id",
        "items_path": "data.items",
        "id_path": "id",
        "name_path": "name",
        "url_path": "url",
        "status_path": "status",
        "display_fields": [],
        "metadata_paths": {"provider": "provider"},
        "enabled": True,
        **overrides,
    }


def approve_all(service: IntegrationManagementService, integration_id: str, names: list[str] | None = None) -> None:
    service.approve_mcp_tools(
        integration_id,
        "primary",
        names or ["listChannels", "getChannel", "createChannel", "updateChannel", "deleteChannel"],
    )


def activate(service: IntegrationManagementService, integration_id: str) -> None:
    service.set_integration_enabled(integration_id, enabled=True)


def discovery(monkeypatch: pytest.MonkeyPatch, service: IntegrationManagementService) -> list[dict[str, Any]]:
    monkeypatch.setattr(service, "_discover_mcp", lambda *_: {"protocol_version": "2025-06-18", "tools": TOOLS, "resources": [{"uri": "mcp://native/readme", "name": "Readme"}]})
    monkeypatch.setattr(service, "_sdk", lambda *_: object())
    return [{"id": "channel-1", "name": "Social page", "url": "https://social.example/channel-1", "status": "READY", "provider": "fixture"}]


def test_approval_is_derived_from_connection_configuration_and_survives_rediscovery(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    assert service.detail(integration_id)["connections"][0]["discovered_tools"][0]["approved"] is False
    approve_all(service, integration_id, ["listChannels"])
    assert service.detail(integration_id)["connections"][0]["discovered_tools"][0]["approved"] is True
    assert all("approved" not in item for item in service.connections.get(integration_id, "primary")["metadata"]["mcp"]["tools"])
    discovery(monkeypatch, service)
    service.set_integration_enabled(integration_id, enabled=True)
    service.discover(integration_id, "primary")
    assert service.detail(integration_id)["connections"][0]["discovered_tools"][0]["approved"] is True
    db.close()


def test_mapping_requires_discovered_approved_tools_and_bounded_data() -> None:
    service, integration_id, db = setup()
    with pytest.raises(PlatformOperationError, match="not been discovered"):
        service.create_mcp_resource_mapping(integration_id, "primary", mapping(list_tool_name="inventedTool"))
    with pytest.raises(PlatformOperationError, match="Approve"):
        service.create_mcp_resource_mapping(integration_id, "primary", mapping(list_tool_name="unapprovedTool", resource_type="other_channel"))
    approve_all(service, integration_id, ["listChannels"])
    service.create_mcp_resource_mapping(integration_id, "primary", mapping())
    with pytest.raises(PlatformConflictError, match="already exists"):
        service.create_mcp_resource_mapping(integration_id, "primary", mapping())
    with pytest.raises(PlatformValidationError, match="scalar"):
        service.create_mcp_resource_mapping(integration_id, "primary", mapping(list_arguments={"nested": {"value": 1}}))
    with pytest.raises(PlatformValidationError, match="template"):
        service.create_mcp_resource_mapping(integration_id, "primary", mapping(list_arguments={"organization": "${secret}"}, resource_type="other_channel"))
    with pytest.raises(PlatformValidationError, match="items_path"):
        service.create_mcp_resource_mapping(integration_id, "primary", mapping(items_path="data.items[0]", resource_type="other_channel"))
    db.close()


def test_mapping_does_not_grant_capabilities_and_discovery_keeps_native_and_mapped_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    approve_all(service, integration_id)
    before = service.integrations.get(integration_id)["granted_capabilities"]
    service.create_mcp_resource_mapping(integration_id, "primary", mapping())
    assert service.integrations.get(integration_id)["granted_capabilities"] == before
    items = discovery(monkeypatch, service)
    monkeypatch.setattr(service.mcp, "call_tool", lambda _sdk, **_kwargs: {"data": {"items": items}})
    activate(service, integration_id)
    result = service.discover(integration_id, "primary")
    assert {item["resource_type"] for item in result["resources"]} == {"mcp_resource", "social_channel"}
    mapped = next(item for item in result["resources"] if item["resource_type"] == "social_channel")
    assert mapped["external_id"] == "channel-1"
    assert set(mapped["available_actions"]) == {"discover", "read", "bind", "refresh", "create", "update", "delete"}
    db.close()


def test_failed_mapped_list_call_preserves_prior_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    approve_all(service, integration_id, ["listChannels"])
    service.create_mcp_resource_mapping(integration_id, "primary", mapping())
    items = discovery(monkeypatch, service)
    monkeypatch.setattr(service.mcp, "call_tool", lambda _sdk, **_kwargs: {"data": {"items": items}})
    activate(service, integration_id)
    service.discover(integration_id, "primary")
    monkeypatch.setattr(service.mcp, "call_tool", lambda _sdk, **_kwargs: (_ for _ in ()).throw(PlatformOperationError("provider unavailable")))
    failed = service.discover(integration_id, "primary")
    current = service.resources(integration_id, connection_id="primary", resource_type="social_channel")["items"]
    assert failed["errors"] and failed["errors"][0]["mapping_id"] == "channels"
    assert current[0]["state"] == "ACTIVE"
    assert service.connections.get(integration_id, "primary")["health"]["state"] == "DEGRADED"
    db.close()


def test_mapping_changes_require_disabled_integration_and_active_bindings_refuse_identity_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    approve_all(service, integration_id, ["listChannels"])
    created = service.create_mcp_resource_mapping(integration_id, "primary", mapping())
    items = discovery(monkeypatch, service)
    monkeypatch.setattr(service.mcp, "call_tool", lambda _sdk, **_kwargs: {"data": {"items": items}})
    activate(service, integration_id)
    inventory = next(item for item in service.discover(integration_id, "primary")["resources"] if item["resource_type"] == "social_channel")
    service.bind(inventory["id"], project_id="p", resource_type="social_channel", resource_id="canonical", role="primary", ownership_mode="linked")
    service.set_integration_enabled(integration_id, enabled=False)
    with pytest.raises(PlatformConflictError, match="Detach active"):
        service.update_mcp_resource_mapping(integration_id, "primary", created["mapping_id"], mapping(id_path="other_id"))
    with pytest.raises(PlatformConflictError, match="Detach active"):
        service.delete_mcp_resource_mapping(integration_id, "primary", created["mapping_id"])
    service.set_integration_enabled(integration_id, enabled=True)
    with pytest.raises(PlatformOperationError, match="Disable"):
        service.update_mcp_resource_mapping(integration_id, "primary", created["mapping_id"], mapping(label="Renamed"))
    db.close()


def test_mapped_actions_require_approval_and_use_external_id_target(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    approve_all(service, integration_id)
    service.create_mcp_resource_mapping(integration_id, "primary", mapping())
    items = discovery(monkeypatch, service)
    calls: list[dict[str, Any]] = []
    def call(_sdk: object, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs["tool_name"] == "listChannels":
            return {"data": {"items": items}}
        return {"ok": True}
    monkeypatch.setattr(service.mcp, "call_tool", call)
    activate(service, integration_id)
    inventory = next(item for item in service.discover(integration_id, "primary")["resources"] if item["resource_type"] == "social_channel")
    binding = service.bind(inventory["id"], project_id="p", resource_type="social_channel", resource_id="canonical", role="primary", ownership_mode="managed")
    read = service.resource_action(integration_id, inventory["id"], action="read")
    assert read["result"]["response"] == {"ok": True}
    assert calls[-1]["tool_name"] == "getChannel" and calls[-1]["arguments"] == {"channel_id": "channel-1"}
    updated = service.resource_action(integration_id, inventory["id"], action="update", arguments={"name": "Renamed"}, expected_revision=binding["revision"])
    assert updated["action"] == "update"
    assert calls[-2]["tool_name"] == "updateChannel" and calls[-2]["arguments"] == {"name": "Renamed", "channel_id": "channel-1"}
    deleted = service.resource_action(integration_id, inventory["id"], action="delete", confirm_destructive=True, expected_revision=updated["binding"]["revision"], idempotency_key="delete-channel-1")
    assert deleted["inventory"]["state"] == "REMOTE_MISSING"
    assert calls[-1]["tool_name"] == "deleteChannel" and calls[-1]["arguments"] == {"channel_id": "channel-1"}
    db.close()
