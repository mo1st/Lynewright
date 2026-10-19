from __future__ import annotations

import sqlite3

import pytest

from app.services.extension_registry import ExtensionRegistry
from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    PlatformValidationError,
)


def service_manifest(extension_id: str = "lynewright.social.mock") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "id": extension_id,
        "name": "Mock Social Provider",
        "version": "0.1.0",
        "type": "service",
        "lynewright_version": "^0.1.0",
        "sdk_version": "^1.0.0",
        "requested_capabilities": ["jobs:create", "network:use"],
        "service": {"kind": "http", "health_check": "/health"},
        "contributions": ["services", "health"],
        "backend": "dist/backend.mjs",
    }


@pytest.fixture()
def store() -> PlatformStore:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    yield store
    connection.close()


def test_register_and_inspect_persist_complete_metadata(store: PlatformStore) -> None:
    registry = ExtensionRegistry(store)
    created = registry.register(
        service_manifest(),
        configuration={"endpoint": "https://postiz.example.com", "credential_ref": "social.postiz.primary"},
    )

    assert created["id"] == "lynewright.social.mock"
    assert created["status"] == "installed"
    assert created["enabled"] is False
    assert created["permission_mode"] == "restricted"
    assert created["requested_capabilities"] == ["jobs:create", "network:use"]
    assert created["granted_capabilities"] == []
    assert created["configuration"]["credential_ref"] == "social.postiz.primary"
    assert created["manifest"]["id"] == created["id"]
    assert registry.inspect(created["id"]) == created


def test_registry_survives_restart_and_disabled_state_persists(store: PlatformStore) -> None:
    first = ExtensionRegistry(store)
    first.register(service_manifest())
    first.enable("lynewright.social.mock")
    disabled = first.disable("lynewright.social.mock")

    after_restart = ExtensionRegistry(store)
    persisted = after_restart.get("lynewright.social.mock")
    assert persisted["status"] == "disabled"
    assert persisted["enabled"] is False
    assert persisted["updated_at"] == disabled["updated_at"]


def test_duplicate_ids_are_tenant_scoped_and_rejected(store: PlatformStore) -> None:
    first_tenant = ExtensionRegistry(store, tenant_id="tenant-a")
    first_tenant.register(service_manifest())
    with pytest.raises(PlatformConflictError):
        first_tenant.register(service_manifest())

    second_tenant = ExtensionRegistry(store, tenant_id="tenant-b")
    assert second_tenant.register(service_manifest())["id"] == "lynewright.social.mock"


def test_invalid_lifecycle_transitions_and_configuration_changes_are_rejected(store: PlatformStore) -> None:
    registry = ExtensionRegistry(store)
    registry.register(service_manifest())
    registry.enable("lynewright.social.mock")

    with pytest.raises(PlatformOperationError, match="already enabled"):
        registry.enable("lynewright.social.mock")
    with pytest.raises(PlatformOperationError, match="Disable an extension"):
        registry.configure("lynewright.social.mock", {"endpoint": "https://postiz.example.com"})
    with pytest.raises(PlatformOperationError, match="Disable an extension"):
        registry.uninstall("lynewright.social.mock")

    registry.disable("lynewright.social.mock")
    with pytest.raises(PlatformOperationError, match="already disabled"):
        registry.disable("lynewright.social.mock")


def test_uninstall_removes_all_metadata(store: PlatformStore) -> None:
    registry = ExtensionRegistry(store)
    registry.register(service_manifest())
    registry.uninstall("lynewright.social.mock")

    assert registry.list() == []
    with pytest.raises(PlatformNotFoundError):
        registry.get("lynewright.social.mock")


def test_configuration_is_json_bounded_and_never_accepts_secrets(store: PlatformStore) -> None:
    registry = ExtensionRegistry(store)
    with pytest.raises(PlatformValidationError, match="reference credentials"):
        registry.register(service_manifest(), configuration={"api_token": "nope"})
    with pytest.raises(PlatformValidationError, match="64 KiB"):
        registry.register(service_manifest(), configuration={"notes": "x" * (64 * 1024)})

    registry.register(service_manifest())
    updated = registry.configure("lynewright.social.mock", {"endpoint": "https://postiz.example.com"})
    assert updated["configuration"] == {"endpoint": "https://postiz.example.com"}
