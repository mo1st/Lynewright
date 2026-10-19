from __future__ import annotations

import sqlite3

import pytest

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_management import IntegrationManagementService
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformOperationError, PlatformStore, PlatformValidationError


INTEGRATION_ID = "lynewright.integration.resource-action-test"


def _setup(*, operations: list[str]) -> tuple[IntegrationManagementService, sqlite3.Connection, list[str]]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    integrations.register({
        "schema_version": "1.2",
        "id": INTEGRATION_ID,
        "name": "Resource action test",
        "version": "0.1.0",
        "type": "service",
        "lynewright_version": "^0.1.0",
        "sdk_version": "^1.0.0",
        "requested_capabilities": [],
        "service": {"kind": "http", "health_check": "/health"},
        "backend": "manual-runtime",
        "management": {
            "resources": [{
                "type": "remote_item",
                "label": "Remote item",
                "plural_label": "Remote items",
                "discoverable": False,
                "operations": operations,
                "operation_contract": "lynewright.test.resource",
                "operation_names": {
                    action: action for action in ("update", "delete") if action in operations
                },
            }],
        },
    })
    integrations.enable(INTEGRATION_ID)
    connections = ConnectionProfileService(
        store, registry=integrations, endpoint_validator=lambda _endpoint: {},
    )
    connections.create(INTEGRATION_ID, connection_id="primary", name="Primary")
    contributions = ContributionRegistry(integrations, connections=connections)
    calls: list[str] = []

    def provider(operation: str, payload: dict) -> dict:
        calls.append(operation)
        if payload["arguments"].get("fail"):
            raise RuntimeError("provider failed")
        return {
            "operation": operation,
            "external_resource_id": payload["external_resource_id"],
            "updated": True,
        }

    contributions.register(
        integration_id=INTEGRATION_ID,
        local_id="resource",
        kind="service",
        handler=provider,
        metadata={
            "contract": "lynewright.test.resource",
            "version": "1.0.0",
            "invocation": "direct",
        },
    )
    service = IntegrationManagementService(integrations, connections, contributions)
    return service, db, calls


def _resource(service: IntegrationManagementService) -> dict:
    return service.inventory.refresh(
        integration_id=INTEGRATION_ID,
        connection_id="primary",
        resource_type="remote_item",
        resources=[{
            "external_id": "remote-1",
            "name": "Remote item",
            "available_actions": ["read", "bind", "update", "delete"],
        }],
    )["items"][0]


def test_resource_actions_require_descriptor_support() -> None:
    service, db, calls = _setup(operations=["read", "bind"])
    resource = _resource(service)

    with pytest.raises(PlatformOperationError, match="not declared"):
        service.resource_action(INTEGRATION_ID, resource["id"], action="update")
    with pytest.raises(PlatformOperationError, match="not declared"):
        service.resource_action(
            INTEGRATION_ID, resource["id"], action="delete",
            confirm_destructive=True, expected_revision=1, idempotency_key="delete-remote-1",
        )
    assert calls == []
    db.close()


def test_managed_update_and_confirmed_delete_use_provider_and_idempotency() -> None:
    service, db, calls = _setup(operations=["read", "bind", "update", "delete"])
    resource = _resource(service)
    binding = service.bind(
        resource["id"], project_id="project-a", resource_type="remote_item",
        resource_id="canonical-1", role="primary", ownership_mode="managed",
    )

    updated = service.resource_action(
        INTEGRATION_ID, resource["id"], action="update",
        arguments={"label": "Updated"}, expected_revision=binding["revision"],
    )
    assert updated["result"]["operation"] == "update"
    assert service.bindings.get(binding["id"])["state"] == "PENDING"

    deleted = service.resource_action(
        INTEGRATION_ID, resource["id"], action="delete",
        confirm_destructive=True, expected_revision=binding["revision"],
        idempotency_key="delete-remote-1",
    )
    assert deleted["binding"]["state"] == "RETIRING"
    assert deleted["inventory"]["state"] == "REMOTE_MISSING"
    assert calls == ["update", "delete"]

    replay = service.resource_action(
        INTEGRATION_ID, resource["id"], action="delete",
        confirm_destructive=True, expected_revision=binding["revision"],
        idempotency_key="delete-remote-1",
    )
    assert replay == deleted
    assert calls == ["update", "delete"]
    db.close()


def test_failed_remote_action_does_not_mutate_binding() -> None:
    service, db, calls = _setup(operations=["read", "bind", "update"])
    resource = _resource(service)
    binding = service.bind(
        resource["id"], project_id="project-a", resource_type="remote_item",
        resource_id="canonical-1", role="primary", ownership_mode="managed",
    )

    with pytest.raises(PlatformOperationError, match="action failed"):
        service.resource_action(
            INTEGRATION_ID, resource["id"], action="update",
            arguments={"fail": True}, expected_revision=binding["revision"],
        )
    current = service.bindings.get(binding["id"])
    assert current["state"] == "PENDING"
    assert current["revision"] == binding["revision"]
    assert calls == ["update"]
    db.close()


def test_observed_and_linked_bindings_cannot_use_remote_mutations() -> None:
    service, db, _calls = _setup(operations=["read", "bind", "update"])
    resource = _resource(service)
    for ownership_mode in ("linked", "observed"):
        binding = service.bind(
            resource["id"], project_id=f"project-{ownership_mode}", resource_type="remote_item",
            resource_id=f"canonical-{ownership_mode}", role="primary", ownership_mode=ownership_mode,
        )
        with pytest.raises(PlatformOperationError, match="managed"):
            service.resource_action(
                INTEGRATION_ID, resource["id"], action="update",
                expected_revision=binding["revision"],
            )
        service.unbind(binding["id"], expected_revision=binding["revision"])
    db.close()
