from __future__ import annotations

import sqlite3

import pytest

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import (
    CredentialService,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    PlatformValidationError,
)


def integration_manifest() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "id": "lynewright.integration.mock-http",
        "name": "Mock HTTP Integration",
        "version": "0.1.0",
        "type": "service",
        "lynewright_version": "^0.1.0",
        "sdk_version": "^1.0.0",
        "requested_capabilities": ["network:use", "credentials:use", "jobs:create"],
        "service": {"kind": "http", "health_check": "/health"},
        "backend": "dist/backend.mjs",
    }


@pytest.fixture()
def connections() -> ConnectionProfileService:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    registry = IntegrationRegistry(store)
    registry.register(integration_manifest())
    credentials = CredentialService(store)
    credentials.set({"name": "mock.primary", "kind": "token", "secret": "test-secret"})
    service = ConnectionProfileService(
        store,
        registry=registry,
        credentials=credentials,
        endpoint_validator=lambda endpoint: {"endpoint": endpoint},
    )
    yield service
    connection.close()


def test_connection_profile_keeps_code_credentials_and_scope_separate(connections: ConnectionProfileService) -> None:
    profile = connections.create(
        "lynewright.integration.mock-http",
        connection_id="primary",
        name="Primary account",
        endpoint_url="https://mock.example.test",
        credential_refs=["mock.primary"],
        project_id="project-a",
        configuration={"region": "eu-west"},
        metadata={"purpose": "test"},
    )

    assert profile["integration_id"] == "lynewright.integration.mock-http"
    assert profile["endpoint_url"] == "https://mock.example.test"
    assert profile["credential_refs"] == ["mock.primary"]
    assert profile["project_id"] == "project-a"
    assert profile["configuration"] == {"region": "eu-west"}
    assert profile["health"] == {"state": "UNKNOWN"}


def test_one_integration_can_have_multiple_profiles_and_survive_restart(connections: ConnectionProfileService) -> None:
    first = connections.create("lynewright.integration.mock-http", name="One", endpoint_url="https://one.example.test")
    second = connections.create("lynewright.integration.mock-http", name="Two", endpoint_url="https://two.example.test")
    assert {profile["id"] for profile in connections.list("lynewright.integration.mock-http")} == {first["id"], second["id"]}

    after_restart = ConnectionProfileService(
        connections.store,
        registry=connections.registry,
        credentials=connections.credentials,
        endpoint_validator=lambda endpoint: {"endpoint": endpoint},
    )
    assert after_restart.get("lynewright.integration.mock-http", first["id"])["name"] == "One"


def test_connections_validate_parent_credentials_endpoint_and_non_secret_configuration(connections: ConnectionProfileService) -> None:
    with pytest.raises(PlatformNotFoundError):
        connections.create("lynewright.integration.missing", name="Missing")
    with pytest.raises(PlatformValidationError, match="credential references"):
        connections.create(
            "lynewright.integration.mock-http", name="Missing credential", credential_refs=["missing.primary"]
        )
    with pytest.raises(PlatformValidationError, match="reference credentials"):
        connections.create(
            "lynewright.integration.mock-http", name="Raw secret", configuration={"api_token": "not-allowed"}
        )


def test_connection_enable_health_configuration_and_remove_lifecycle(connections: ConnectionProfileService) -> None:
    profile = connections.create("lynewright.integration.mock-http", name="Primary")
    disabled = connections.set_enabled("lynewright.integration.mock-http", profile["id"], enabled=False)
    assert disabled["enabled"] is False
    with pytest.raises(PlatformOperationError, match="already in the requested"):
        connections.set_enabled("lynewright.integration.mock-http", profile["id"], enabled=False)

    configured = connections.configure(
        "lynewright.integration.mock-http",
        profile["id"],
        endpoint_url="https://changed.example.test",
        configuration={"mode": "test"},
    )
    assert configured["configuration"] == {"mode": "test"}

    healthy = connections.record_health(
        "lynewright.integration.mock-http", profile["id"], state="HEALTHY", latency_ms=24, remote_version="v1"
    )
    assert healthy["health"] == {"state": "HEALTHY", "latency_ms": 24, "remote_version": "v1", "error_code": ""}
    assert healthy["last_health_check_at"] is not None

    connections.remove("lynewright.integration.mock-http", profile["id"])
    with pytest.raises(PlatformNotFoundError):
        connections.get("lynewright.integration.mock-http", profile["id"])
