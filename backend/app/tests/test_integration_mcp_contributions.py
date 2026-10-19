from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from app.services.integration_capabilities import IntegrationAuthorizationService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_mcp_contributions import IntegrationMCPContributionBridge
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import PlatformOperationError, PlatformStore, PlatformValidationError


def manifest(integration_id: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0", "id": integration_id, "name": "Domain Mock", "version": "0.1.0",
        "type": "service", "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0",
        "requested_capabilities": ["events:publish"], "service": {"kind": "http", "health_check": "/health"}, "backend": "dist/backend.mjs",
    }


@pytest.fixture()
def contribution_setup() -> tuple[ContributionRegistry, IntegrationMCPContributionBridge, CapabilityActor, list[dict[str, Any]]]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    integration_id = "lynewright.integration.domain-mock"
    integrations.register(manifest(integration_id))
    integrations.enable(integration_id)
    authorization = IntegrationAuthorizationService(integrations)
    authorization.set_permissions(integration_id, permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager())
    contributions = ContributionRegistry(integrations)
    factory = IntegrationSDKFactory(store, authorization=authorization)
    bridge = IntegrationMCPContributionBridge(contributions, factory)
    client = CapabilityActor("domain-mcp-caller", "Domain MCP caller", "mcp", "custom", frozenset({"jobs:create"}))
    seen: list[dict[str, Any]] = []
    yield contributions, bridge, client, seen
    db.close()


def metadata(**overrides: Any) -> dict[str, Any]:
    value = {
        "name": "lynewright.integration.domain.publish", "description": "Publish a domain item.",
        "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}},
        "caller_capability": "jobs:create", "integration_capability": "events:publish", "project_scope": "none",
    }
    return value | overrides


def test_mcp_contribution_requires_explicit_complete_metadata(contribution_setup: tuple[ContributionRegistry, IntegrationMCPContributionBridge, CapabilityActor, list[dict[str, Any]]]) -> None:
    contributions, _, _, _ = contribution_setup
    with pytest.raises(PlatformValidationError, match="input_schema"):
        contributions.register(integration_id="lynewright.integration.domain-mock", local_id="invalid", kind="mcp", handler=lambda *_: {}, metadata={"name": "lynewright.integration.domain.invalid", "description": "Missing fields"})


def test_mcp_contribution_preserves_caller_and_integration_controls(contribution_setup: tuple[ContributionRegistry, IntegrationMCPContributionBridge, CapabilityActor, list[dict[str, Any]]]) -> None:
    contributions, bridge, caller, seen = contribution_setup
    contributions.register(
        integration_id="lynewright.integration.domain-mock", local_id="publish", kind="mcp", metadata=metadata(),
        handler=lambda sdk, arguments: seen.append({"integration_id": sdk.integration_id, "caller_id": sdk.caller.client_id, "arguments": arguments}) or {"accepted": True},
    )

    definition = bridge.definitions()[0]
    assert definition.name == "lynewright.integration.domain.publish"
    assert bridge.invoke(definition.contribution_id, caller, {"value": "hello"}) == {"accepted": True}
    assert seen == [{"integration_id": "lynewright.integration.domain-mock", "caller_id": caller.client_id, "arguments": {"value": "hello"}}]

    denied = CapabilityActor("restricted", "Restricted", "test", "restricted", frozenset({"system:read"}))
    with pytest.raises(PlatformOperationError, match="not authorized"):
        bridge.invoke(definition.contribution_id, denied, {"value": "blocked"})


def test_mcp_contribution_is_removed_when_its_integration_is_disabled(contribution_setup: tuple[ContributionRegistry, IntegrationMCPContributionBridge, CapabilityActor, list[dict[str, Any]]]) -> None:
    contributions, bridge, caller, _ = contribution_setup
    contributions.register(integration_id="lynewright.integration.domain-mock", local_id="publish", kind="mcp", metadata=metadata(), handler=lambda *_: {"ok": True})
    definition = bridge.definitions()[0]
    assert bridge.invoke(definition.contribution_id, caller, {}) == {"ok": True}
    contributions.disable_integration("lynewright.integration.domain-mock")
    assert bridge.definitions() == []


def test_mcp_contribution_never_exposes_external_mcp_discovery_as_a_tool(contribution_setup: tuple[ContributionRegistry, IntegrationMCPContributionBridge, CapabilityActor, list[dict[str, Any]]]) -> None:
    contributions, bridge, _, _ = contribution_setup
    contributions.register(integration_id="lynewright.integration.domain-mock", local_id="publish", kind="mcp", metadata=metadata(), handler=lambda *_: {"ok": True})
    names = {item.name for item in bridge.definitions()}
    assert "approved.echo" not in names
    assert "lynewright.integration.domain.publish" in names
