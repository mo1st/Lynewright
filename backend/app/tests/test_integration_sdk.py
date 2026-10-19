from __future__ import annotations

import sqlite3

import pytest

from app.services.capability_context import CapabilityInvocationContext
from app.services.integration_capabilities import IntegrationAuthorizationService
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.platform_capabilities import CapabilityActor, ClientAuthorizationService
from app.services.platform_services import CredentialService, PlatformOperationError, PlatformStore


def manifest() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "id": "lynewright.integration.sdk-mock",
        "name": "SDK Mock",
        "version": "0.1.0",
        "type": "native",
        "lynewright_version": "^0.1.0",
        "sdk_version": "^1.0.0",
        "requested_capabilities": [
            "projects:read", "jobs:create", "jobs:read", "media:read", "media:create",
            "credentials:list", "credentials:status", "credentials:use", "ai:use", "network:use",
            "settings:read", "settings:write", "events:publish", "storage:read", "storage:write",
            "mappings:read", "mappings:write", "sync:read", "sync:write",
        ],
        "backend": "dist/backend.mjs",
    }


@pytest.fixture()
def sdk_setup() -> tuple[PlatformStore, IntegrationRegistry, IntegrationAuthorizationService, ClientAuthorizationService]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE tenant_projects (
            project_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(tenant_id,name)
        );
        INSERT INTO tenant_projects (project_id,tenant_id,name) VALUES ('project-a','ten_default','Project A');
        """
    )
    store = PlatformStore(lambda: connection)
    registry = IntegrationRegistry(store)
    registry.register(manifest())
    registry.enable("lynewright.integration.sdk-mock")
    authorization = IntegrationAuthorizationService(registry)
    clients = ClientAuthorizationService(store)
    CredentialService(store).set({"name": "sdk.primary", "kind": "token", "secret": "secret-value"})
    yield store, registry, authorization, clients
    connection.close()


def caller(clients: ClientAuthorizationService, *, mode: str = "full", grants: list[str] | None = None) -> CapabilityActor:
    provisioned = clients.create_client(
        {"name": f"Caller {mode} {len(grants or [])}", "permission_mode": mode, "granted_capabilities": grants or []},
        actor=CapabilityActor.platform_manager(),
    )
    return clients.authenticate(provisioned["access_token"])


def factory(store: PlatformStore, authorization: IntegrationAuthorizationService) -> IntegrationSDKFactory:
    return IntegrationSDKFactory(
        store,
        authorization=authorization,
        project_scope=lambda *_: True,
    )


def test_sdk_exposes_scoped_facades_and_preserves_audit_context(sdk_setup: tuple[PlatformStore, IntegrationRegistry, IntegrationAuthorizationService, ClientAuthorizationService]) -> None:
    store, _, authorization, clients = sdk_setup
    authorization.set_permissions(
        "lynewright.integration.sdk-mock", permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager()
    )
    sdk = factory(store, authorization).create(
        caller=caller(clients), integration_id="lynewright.integration.sdk-mock", project_id="project-a", request_id="req-1"
    )

    assert not hasattr(sdk, "store")
    assert sdk.audit_context.as_dict() == {
        "caller_id": sdk.caller.client_id,
        "integration_id": "lynewright.integration.sdk-mock",
        "project_id": "project-a",
        "request_id": "req-1",
    }
    assert sdk.storage.set("cursor", {"value": 1}) == {"value": 1}
    assert sdk.storage.get("cursor") == {"value": 1}
    assert sdk.storage.keys() == ["cursor"]
    assert sdk.mappings.put(external_type="account", external_id="remote-1", lynewright_type="project", lynewright_id="project-a")["lynewright_id"] == "project-a"
    assert sdk.mappings.get("account", "remote-1")["external_id"] == "remote-1"
    assert sdk.sync_state.set({"cursor": "next"})["state"] == {"cursor": "next"}
    assert sdk.events.publish("sync.completed", {"count": 1})["type"] == "integration.lynewright.integration.sdk-mock.sync.completed"
    assert sdk.settings.set("mode", "test")["value"] == "test"
    assert sdk.settings.get("mode")["key"].endswith(".mode")
    assert sdk.credentials.status("sdk.primary")["configured"] is True
    assert sdk.credentials.use("sdk.primary", lambda value: {"length": len(value)}) == {"length": 12}
    with pytest.raises(PlatformOperationError):
        sdk.credentials.use("sdk.primary", lambda value: value)
    with pytest.raises(PlatformOperationError):
        sdk.credentials.use("sdk.primary", lambda value: {"nested": [value]})


def test_sdk_requires_both_caller_and_integration_capabilities(sdk_setup: tuple[PlatformStore, IntegrationRegistry, IntegrationAuthorizationService, ClientAuthorizationService]) -> None:
    store, _, authorization, clients = sdk_setup
    authorization.set_permissions(
        "lynewright.integration.sdk-mock",
        permission_mode="custom",
        granted_capabilities=["storage:write"],
        actor=CapabilityActor.platform_manager(),
    )
    full_caller_sdk = factory(store, authorization).create(caller=caller(clients), integration_id="lynewright.integration.sdk-mock")
    full_caller_sdk.storage.set("allowed", True)
    with pytest.raises(PlatformOperationError):
        full_caller_sdk.storage.get("allowed")
    with pytest.raises(PlatformOperationError):
        full_caller_sdk.jobs.create({"type": "integration.sdk"})

    authorization.set_permissions(
        "lynewright.integration.sdk-mock", permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager()
    )
    restricted_caller_sdk = factory(store, authorization).create(
        caller=caller(clients, mode="custom", grants=["storage:read"]), integration_id="lynewright.integration.sdk-mock"
    )
    with pytest.raises(PlatformOperationError):
        restricted_caller_sdk.storage.set("caller-denied", True)


def test_sdk_project_scope_and_disabled_integration_are_enforced(sdk_setup: tuple[PlatformStore, IntegrationRegistry, IntegrationAuthorizationService, ClientAuthorizationService]) -> None:
    store, registry, authorization, clients = sdk_setup
    authorization.set_permissions(
        "lynewright.integration.sdk-mock", permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager()
    )
    full_caller = caller(clients)
    denied = IntegrationSDKFactory(store, authorization=authorization, project_scope=lambda *_: False).create(
        caller=full_caller, integration_id="lynewright.integration.sdk-mock", project_id="project-a"
    )
    with pytest.raises(PlatformOperationError, match="Project scope denied"):
        denied.jobs.create({"type": "integration.sdk"})

    registry.disable("lynewright.integration.sdk-mock")
    with pytest.raises(PlatformOperationError, match="Integration is disabled"):
        factory(store, authorization).create(caller=full_caller, integration_id="lynewright.integration.sdk-mock")


def test_default_project_scope_is_tenant_bound_and_active_only(sdk_setup: tuple[PlatformStore, IntegrationRegistry, IntegrationAuthorizationService, ClientAuthorizationService]) -> None:
    store, _, authorization, clients = sdk_setup
    authorization.set_permissions(
        "lynewright.integration.sdk-mock", permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager()
    )
    sdk = IntegrationSDKFactory(store, authorization=authorization).create(
        caller=caller(clients), integration_id="lynewright.integration.sdk-mock", project_id="project-a"
    )
    assert sdk.storage.set("scope-check", True) is True

    with pytest.raises(PlatformOperationError, match="Project scope denied"):
        IntegrationSDKFactory(store, authorization=authorization).create(
            caller=CapabilityActor("scope-checker", "Scope checker", "test", "full", frozenset()),
            integration_id="lynewright.integration.sdk-mock", project_id="other-project"
        ).storage.set("scope-check", True)

    with store.connection() as db, db:
        db.execute("UPDATE tenant_projects SET status='archived' WHERE project_id='project-a'")
    with pytest.raises(PlatformOperationError, match="Project scope denied"):
        sdk.storage.set("archived-check", True)


def test_disabled_mapping_preview_keeps_capability_intersection(sdk_setup: tuple[PlatformStore, IntegrationRegistry, IntegrationAuthorizationService, ClientAuthorizationService]) -> None:
    store, registry, authorization, clients = sdk_setup
    authorization.set_permissions(
        "lynewright.integration.sdk-mock", permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager()
    )
    registry.disable("lynewright.integration.sdk-mock")
    context = CapabilityInvocationContext.from_actor(
        tenant_id="ten_default", actor_type="operator", actor_id="integration-management",
        entry_point="integration-management.mapping-preview", integration_id="lynewright.integration.sdk-mock",
    )
    full_caller = caller(clients)
    preview = factory(store, authorization).create(
        caller=full_caller, integration_id="lynewright.integration.sdk-mock",
        invocation_context=context, allow_disabled_integration_for_preview=True,
    )
    preview._allow("network:use")
    with pytest.raises(PlatformOperationError, match="Integration is disabled"):
        factory(store, authorization).create(
            caller=full_caller, integration_id="lynewright.integration.sdk-mock",
            invocation_context=context,
        )
