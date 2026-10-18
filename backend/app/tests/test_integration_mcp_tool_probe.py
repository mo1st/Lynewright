from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_management import IntegrationManagementService
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformNotFoundError, PlatformOperationError, PlatformStore, PlatformValidationError


READ_TOOL = {"name": "getAccount", "description": "Read account", "read_only": True, "destructive": False, "safety_known": True}
WRITE_TOOL = {"name": "createPost", "description": "Create post", "read_only": False, "destructive": True, "safety_known": True}


class Logger:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write(self, level: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.events.append({"level": level, "message": message, "details": details or {}})


class SDK:
    def __init__(self) -> None:
        self.logger = Logger()


def setup() -> tuple[IntegrationManagementService, str, sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    connections = ConnectionProfileService(store, registry=integrations, endpoint_validator=lambda _: {})
    service = IntegrationManagementService(integrations, connections, ContributionRegistry(integrations, connections=connections))
    integration_id = service.create_manual(
        name="MCP probe fixture", runtime_kind="mcp", endpoint_url="https://mcp.example.test",
    )["integration"]["id"]
    service.set_authorization(integration_id, permission_mode="custom", granted_capabilities=["network:use"])
    service.connections.configure(integration_id, "primary", metadata={"mcp": {"tools": [READ_TOOL, WRITE_TOOL]}})
    return service, integration_id, db


def approve(service: IntegrationManagementService, integration_id: str, names: list[str]) -> None:
    service.approve_mcp_tools(integration_id, "primary", names)


def test_probe_requires_discovery_approval_and_explicit_read_safety() -> None:
    service, integration_id, db = setup()
    with pytest.raises(PlatformNotFoundError, match="not been discovered"):
        service.probe_mcp_tool(integration_id, "primary", "missing", {})
    approve(service, integration_id, [READ_TOOL["name"], WRITE_TOOL["name"]])
    with pytest.raises(PlatformOperationError, match="explicitly marked read-only"):
        service.probe_mcp_tool(integration_id, "primary", WRITE_TOOL["name"], {})
    service.connections.configure(integration_id, "primary", metadata={"mcp": {"tools": [{"name": "unknownSafety"}]}})
    approve(service, integration_id, ["unknownSafety"])
    with pytest.raises(PlatformOperationError, match="explicitly marked read-only"):
        service.probe_mcp_tool(integration_id, "primary", "unknownSafety", {})
    db.close()


def test_probe_reuses_bounded_scalar_argument_rules_and_redacts_large_results(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    approve(service, integration_id, [READ_TOOL["name"]])
    sdk = SDK()
    monkeypatch.setattr(service, "_sdk", lambda *_: sdk)
    monkeypatch.setattr(
        service.mcp,
        "call_tool",
        lambda *_args, **_kwargs: {"organizations": [{"id": "org-1", "api_token": "never-return-this"}], "padding": "x" * (300 * 1024)},
    )
    with pytest.raises(PlatformValidationError, match="scalar"):
        service.probe_mcp_tool(integration_id, "primary", READ_TOOL["name"], {"nested": {"id": "org-1"}})
    with pytest.raises(PlatformValidationError, match="template"):
        service.probe_mcp_tool(integration_id, "primary", READ_TOOL["name"], {"organization": "{{id}}"})
    result = service.probe_mcp_tool(integration_id, "primary", READ_TOOL["name"], {"organization": "org-1"})
    assert result["truncated"] is True
    assert result["serialized_bytes"] > 256 * 1024
    assert "never-return-this" not in str(result["result"])
    assert result["result"]["_truncated"] is True
    assert sdk.logger.events[0]["details"]["arguments"] == {"organization": "org-1"}
    db.close()


def test_live_input_schema_is_bounded_redacted_audited_and_never_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    service, integration_id, db = setup()
    sdk = SDK()
    before = service.connections.get(integration_id, "primary")["metadata"]
    monkeypatch.setattr(service, "_sdk", lambda *_: sdk)
    monkeypatch.setattr(
        service.mcp,
        "tool_input_schema",
        lambda *_args, **_kwargs: {
            "type": "object",
            "properties": {"channelId": {"type": "string"}, "api_token": {"default": "never-return-this"}},
        },
    )

    result = service.mcp_tool_input_schema(integration_id, "primary", WRITE_TOOL["name"])

    assert result["input_schema"]["properties"]["channelId"] == {"type": "string"}
    assert "never-return-this" not in str(result["input_schema"])
    assert result["truncated"] is False
    assert service.connections.get(integration_id, "primary")["metadata"] == before
    assert sdk.logger.events[0]["details"]["event"] == "mcp_tool_schema_view"
    db.close()
