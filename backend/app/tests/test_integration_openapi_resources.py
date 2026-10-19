from __future__ import annotations

import sqlite3

import pytest

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_management import IntegrationManagementService
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformConflictError, PlatformOperationError, PlatformStore, PlatformValidationError


DOCUMENT = {
    "openapi": "3.0.3",
    "info": {"title": "Fixture API", "version": "1.0.0"},
    "paths": {
        "/objects": {
            "get": {"operationId": "listObjects", "responses": {"200": {}}},
            "post": {"operationId": "createObject", "responses": {"200": {}}},
        },
        "/things": {"get": {"operationId": "listThings", "responses": {"200": {}}}},
        "/objects/{id}": {
            "get": {"operationId": "getObject", "responses": {"200": {}}},
            "patch": {"operationId": "updateObject", "responses": {"200": {}}},
            "delete": {"operationId": "deleteObject", "responses": {"204": {}}},
        },
    },
}


def setup() -> tuple[IntegrationManagementService, str, sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    connections = ConnectionProfileService(store, registry=integrations, endpoint_validator=lambda _: {})
    service = IntegrationManagementService(integrations, connections, ContributionRegistry(integrations, connections=connections))
    integration_id = service.create_manual(
        name="Fixture API", runtime_kind="openapi", endpoint_url="https://fixture.example.test", document=DOCUMENT,
    )["integration"]["id"]
    return service, integration_id, db


def mapping(**values: object) -> dict[str, object]:
    return {
        "resource_type": "universe_object",
        "label": "Universe object",
        "plural_label": "Universe objects",
        "list_operation_id": "listObjects",
        "items_path": "data.items",
        "id_path": "id",
        "name_path": "name",
        "parent_id_path": "parent.id",
        "url_path": "links.self",
        "metadata_paths": {"kind": "kind"},
        **values,
    }


def activate(service: IntegrationManagementService, integration_id: str, connection_id: str = "primary") -> None:
    service.approve_openapi_operations(integration_id, connection_id, ["listObjects"])
    service.set_integration_enabled(integration_id, enabled=True)


def test_openapi_mapping_is_data_only_and_rejects_invalid_shapes() -> None:
    service, integration_id, db = setup()
    created = service.create_openapi_resource_mapping(integration_id, mapping())
    assert created["resource_type"] == "universe_object"
    assert service.openapi_resource_mappings(integration_id)[0]["metadata_paths"] == {"kind": "kind"}
    with pytest.raises(PlatformValidationError, match="unknown list operation"):
        service.create_openapi_resource_mapping(integration_id, mapping(resource_type="other_object", list_operation_id="invented"))
    with pytest.raises(PlatformValidationError, match="id_path"):
        service.create_openapi_resource_mapping(integration_id, mapping(resource_type="other_object", id_path="id[0]"))
    with pytest.raises(PlatformValidationError, match="metadata paths"):
        service.create_openapi_resource_mapping(integration_id, mapping(resource_type="other_object", metadata_paths={"description": "token"}))
    with pytest.raises(PlatformConflictError, match="already exists"):
        service.create_openapi_resource_mapping(integration_id, mapping())
    db.close()


def test_openapi_discovery_requires_approval_and_populates_connection_scoped_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    service.create_openapi_resource_mapping(integration_id, mapping())
    service.set_integration_enabled(integration_id, enabled=True)
    unapproved = service.discover(integration_id, "primary")
    assert unapproved["resources"] == []
    assert "Approve listObjects" in unapproved["errors"][0]["message"]

    service.set_integration_enabled(integration_id, enabled=False)
    service.approve_openapi_operations(integration_id, "primary", ["listObjects"])
    service.connections.create(integration_id, connection_id="secondary", name="Secondary", endpoint_url="https://second.example.test")
    service.approve_openapi_operations(integration_id, "secondary", ["listObjects"])
    service.set_integration_enabled(integration_id, enabled=True)
    monkeypatch.setattr(service, "execute_openapi", lambda _i, connection_id, _operation, _args: {"data": {"items": [{"id": "shared", "name": connection_id, "parent": {"id": "parent"}, "links": {"self": "https://objects/shared"}, "kind": "object", "token": "redact-me"}]}})

    first = service.discover(integration_id, "primary")
    second = service.discover(integration_id, "secondary")
    assert first["resources"][0]["connection_id"] == "primary"
    assert second["resources"][0]["connection_id"] == "secondary"
    assert first["resources"][0]["id"] != second["resources"][0]["id"]
    assert first["resources"][0]["parent_external_id"] == "parent"
    assert service.resources(integration_id, connection_id="primary")["items"][0]["metadata"]["kind"] == "object"
    db.close()


def test_missing_identity_and_failed_refresh_do_not_invent_or_destroy_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    service.create_openapi_resource_mapping(integration_id, mapping())
    activate(service, integration_id)
    monkeypatch.setattr(service, "execute_openapi", lambda *_: {"data": {"items": [{"id": "stable", "name": "Stable"}, {"name": "No id"}]}})
    assert [item["external_id"] for item in service.discover(integration_id, "primary")["resources"]] == ["stable"]
    monkeypatch.setattr(service, "execute_openapi", lambda *_: (_ for _ in ()).throw(PlatformOperationError("provider failed")))
    failed = service.discover(integration_id, "primary")
    assert failed["errors"] and service.resources(integration_id, connection_id="primary")["items"][0]["state"] == "ACTIVE"
    db.close()


def test_mapping_does_not_expand_connection_operation_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    service.create_openapi_resource_mapping(integration_id, mapping(delete_operation_id="deleteObject", delete_external_id_target="path.id"))
    activate(service, integration_id)
    monkeypatch.setattr(service, "execute_openapi", lambda *_: {"data": {"items": [{"id": "stable", "name": "Stable"}]}})
    resource = service.discover(integration_id, "primary")["resources"][0]
    assert "delete" not in resource["available_actions"]
    db.close()


def test_one_mapping_failure_preserves_that_inventory_without_staling_others(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    service.create_openapi_resource_mapping(integration_id, mapping())
    service.create_openapi_resource_mapping(integration_id, mapping(resource_type="space_object", label="Space object", plural_label="Space objects", list_operation_id="listThings"))
    service.approve_openapi_operations(integration_id, "primary", ["listObjects", "listThings"])
    service.set_integration_enabled(integration_id, enabled=True)
    monkeypatch.setattr(service, "execute_openapi", lambda _i, _c, operation, _args: {"data": {"items": [{"id": operation, "name": operation}]}})
    service.discover(integration_id, "primary")
    def second_run(_i: str, _c: str, operation: str, _args: dict[str, object]) -> dict[str, object]:
        if operation == "listObjects":
            raise PlatformOperationError("objects unavailable")
        return {"data": {"items": [{"id": "fresh", "name": "Fresh"}]}}
    monkeypatch.setattr(service, "execute_openapi", second_run)
    result = service.discover(integration_id, "primary")
    assert len(result["errors"]) == 1
    current = service.resources(integration_id, connection_id="primary")["items"]
    assert {item["resource_type"] for item in current if item["state"] == "ACTIVE"} == {"universe_object", "space_object"}
    db.close()


def test_mapping_identity_change_or_delete_refuses_active_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    created = service.create_openapi_resource_mapping(integration_id, mapping())
    activate(service, integration_id)
    monkeypatch.setattr(service, "execute_openapi", lambda *_: {"data": {"items": [{"id": "stable", "name": "Stable"}]}})
    inventory = service.discover(integration_id, "primary")["resources"][0]
    service.bind(inventory["id"], project_id="p", resource_type="universe_object", resource_id="canonical", role="primary", ownership_mode="linked")
    service.set_integration_enabled(integration_id, enabled=False)
    with pytest.raises(PlatformConflictError, match="Detach active"):
        service.update_openapi_resource_mapping(integration_id, created["mapping_id"], mapping(id_path="other_id"))
    with pytest.raises(PlatformConflictError, match="Detach active"):
        service.delete_openapi_resource_mapping(integration_id, created["mapping_id"])
    db.close()


def test_openapi_pagination_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    service.create_openapi_resource_mapping(integration_id, mapping(pagination={"cursor_query_param": "cursor", "next_cursor_path": "data.next", "max_pages": 2, "max_resources": 2}))
    activate(service, integration_id)
    calls: list[dict[str, object]] = []
    def execute(_integration: str, _connection: str, _operation: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append(arguments)
        return {"data": {"items": [{"id": str(len(calls)), "name": "Item"}], "next": "next"}}
    monkeypatch.setattr(service, "execute_openapi", execute)
    assert len(service.discover(integration_id, "primary")["resources"]) == 2
    assert calls == [{}, {"query": {"cursor": "next"}}]
    db.close()


def action_ready_resource(monkeypatch: pytest.MonkeyPatch, *, ownership_mode: str = "managed") -> tuple[IntegrationManagementService, str, sqlite3.Connection, dict[str, object], dict[str, object]]:
    service, integration_id, db = setup()
    service.create_openapi_resource_mapping(integration_id, mapping(
        get_operation_id="getObject", get_external_id_target="path.id",
        update_operation_id="updateObject", update_external_id_target="path.id",
        delete_operation_id="deleteObject", delete_external_id_target="path.id",
        create_operation_id="createObject",
    ))
    service.approve_openapi_operations(integration_id, "primary", ["listObjects", "getObject", "updateObject", "deleteObject", "createObject"])
    service.set_integration_enabled(integration_id, enabled=True)
    calls: list[dict[str, object]] = []
    def execute(_integration: str, _connection: str, operation: str, arguments: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append({"operation": operation, "arguments": arguments})
        if operation == "listObjects":
            return {"data": {"items": [{"id": "stable", "name": "Stable"}]}}
        return {"id": "stable", "name": "Changed"}
    monkeypatch.setattr(service, "execute_openapi", execute)
    inventory = service.discover(integration_id, "primary")["resources"][0]
    binding = service.bind(inventory["id"], project_id="p", resource_type="universe_object", resource_id="canonical", role="primary", ownership_mode=ownership_mode)
    return service, integration_id, db, inventory, {"binding": binding, "calls": calls}


def test_openapi_mapped_read_uses_selected_connection_and_requires_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db, inventory, state = action_ready_resource(monkeypatch)
    result = service.resource_action(integration_id, inventory["id"], action="read")
    assert result["result"]["response"] == {"id": "stable", "name": "Changed"}
    assert state["calls"][-1] == {"operation": "getObject", "arguments": {"path": {"id": "stable"}}}
    db.close()


def test_openapi_create_requires_declared_and_approved_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    service.create_openapi_resource_mapping(integration_id, mapping())
    activate(service, integration_id)
    with pytest.raises(PlatformOperationError, match="does not declare"):
        service.create_openapi_resource(integration_id, "primary", "universe_object", arguments={})
    service.set_integration_enabled(integration_id, enabled=False)
    service.update_openapi_resource_mapping(integration_id, service.openapi_resource_mappings(integration_id)[0]["mapping_id"], mapping(create_operation_id="createObject"))
    service.set_integration_enabled(integration_id, enabled=True)
    with pytest.raises(PlatformOperationError, match="Approve"):
        service.create_openapi_resource(integration_id, "primary", "universe_object", arguments={})
    db.close()


def test_openapi_create_refreshes_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db, _inventory, state = action_ready_resource(monkeypatch)
    result = service.create_openapi_resource(integration_id, "primary", "universe_object", arguments={"body": {"name": "New"}})
    assert result["action"] == "create"
    assert any(call["operation"] == "createObject" for call in state["calls"])
    assert any(call["operation"] == "listObjects" for call in state["calls"])
    db.close()


def test_openapi_update_requires_managed_binding_and_expected_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db, inventory, state = action_ready_resource(monkeypatch, ownership_mode="linked")
    with pytest.raises(PlatformOperationError, match="Only managed"):
        service.resource_action(integration_id, inventory["id"], action="update", arguments={"body": {"name": "Changed"}}, expected_revision=1)
    db.close()
    service, integration_id, db, inventory, state = action_ready_resource(monkeypatch)
    with pytest.raises(PlatformValidationError, match="expected revision"):
        service.resource_action(integration_id, inventory["id"], action="update", arguments={"body": {"name": "Changed"}})
    binding = state["binding"]
    result = service.resource_action(integration_id, inventory["id"], action="update", arguments={"body": {"name": "Changed"}}, expected_revision=binding["revision"])
    assert result["action"] == "update" and state["calls"][-2]["operation"] == "updateObject"
    db.close()


def test_openapi_delete_requires_managed_binding_confirmation_idempotency_and_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db, inventory, state = action_ready_resource(monkeypatch)
    binding = state["binding"]
    with pytest.raises(PlatformValidationError, match="explicit confirmation"):
        service.resource_action(integration_id, inventory["id"], action="delete", expected_revision=binding["revision"], idempotency_key="delete-key-123")
    with pytest.raises(PlatformValidationError, match="idempotency"):
        service.resource_action(integration_id, inventory["id"], action="delete", confirm_destructive=True, expected_revision=binding["revision"])
    result = service.resource_action(integration_id, inventory["id"], action="delete", confirm_destructive=True, expected_revision=binding["revision"], idempotency_key="delete-key-123")
    assert result["inventory"]["state"] == "REMOTE_MISSING" and state["calls"][-1]["operation"] == "deleteObject"
    db.close()
