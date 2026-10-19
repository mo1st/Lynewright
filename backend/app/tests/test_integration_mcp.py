from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from app.services.integration_capabilities import IntegrationAuthorizationService
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_mcp import IntegrationMCPAdapter
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.platform_capabilities import CapabilityActor, ClientAuthorizationService
from app.services.platform_services import CredentialService, PlatformOperationError, PlatformStore


class MockMCPHTTP:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def validate_public_url(self, url: str) -> dict[str, str]:
        if "localhost" in url:
            raise PlatformOperationError("private URL")
        return {"url": url}

    def request(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"url": url, **kwargs})
        method = kwargs["json_body"].get("method")
        payloads = {
            "initialize": {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}},
            "notifications/initialized": {},
            "tools/list": {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "approved.echo", "description": "Echo", "inputSchema": {"type": "object"}}, {"name": "unapproved.read"}]}},
            "resources/list": {"jsonrpc": "2.0", "id": 3, "result": {"resources": [{"uri": "mcp://docs/readme", "name": "Readme", "mimeType": "text/plain"}]}},
            "tools/call": {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "{\"ok\": true}"}]}},
        }
        body = payloads[method]
        status = 202 if method == "notifications/initialized" else 200
        headers = {"MCP-Session-Id": "session-1"} if method == "initialize" else {}
        return {"status_code": status, "content_type": "application/json", "headers": headers, "body": __import__("json").dumps(body).encode()}


@pytest.fixture()
def mcp_setup() -> tuple[IntegrationMCPAdapter, IntegrationSDKFactory, ClientAuthorizationService, MockMCPHTTP]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    integrations.register({
        "schema_version": "1.0", "id": "lynewright.integration.mcp-mock", "name": "MCP Mock", "version": "0.1.0",
        "type": "service", "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0",
        "requested_capabilities": ["network:use", "credentials:use"], "service": {"kind": "http", "health_check": "/health"}, "backend": "dist/backend.mjs",
    })
    integrations.enable("lynewright.integration.mcp-mock")
    authorization = IntegrationAuthorizationService(integrations)
    authorization.set_permissions("lynewright.integration.mcp-mock", permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager())
    credentials = CredentialService(store)
    credentials.set({"name": "mcp.token", "kind": "token", "secret": "mcp-secret"})
    http = MockMCPHTTP()
    connections = ConnectionProfileService(store, registry=integrations, credentials=credentials, endpoint_validator=http.validate_public_url)
    connections.create(
        "lynewright.integration.mcp-mock", connection_id="primary", name="Primary", endpoint_url="https://mcp.example.test/rpc",
        credential_refs=["mcp.token"], configuration={"mcp": {"approved_tools": ["approved.echo"]}},
    )
    clients = ClientAuthorizationService(store)
    yield IntegrationMCPAdapter(connections), IntegrationSDKFactory(store, authorization=authorization, connections=connections, http=http), clients, http
    db.close()


def caller(clients: ClientAuthorizationService) -> CapabilityActor:
    value = clients.create_client({"name": "MCP caller", "permission_mode": "full"}, actor=CapabilityActor.platform_manager())
    return clients.authenticate(value["access_token"])


def test_external_mcp_discovery_is_scoped_and_does_not_expose_tools(mcp_setup: tuple[IntegrationMCPAdapter, IntegrationSDKFactory, ClientAuthorizationService, MockMCPHTTP]) -> None:
    adapter, factory, clients, http = mcp_setup
    sdk = factory.create(caller=caller(clients), integration_id="lynewright.integration.mcp-mock", connection_id="primary")

    discovered = adapter.discover(sdk)
    assert discovered["protocol_version"] == "2025-06-18"
    assert discovered["tools"][0]["approved"] is True
    assert discovered["tools"][1]["approved"] is False
    assert discovered["resources"] == [{"uri": "mcp://docs/readme", "name": "Readme", "description": "", "mime_type": "text/plain"}]
    assert http.calls[0]["headers"]["Authorization"] == "Bearer mcp-secret"
    assert "mcp_tools" not in discovered


def test_external_mcp_tool_metadata_bounds_untrusted_input_schema() -> None:
    schema = {"type": "object", "properties": {f"field_{index}": {"type": "string"} for index in range(10_000)}}
    metadata = IntegrationMCPAdapter._tool_metadata(
        {"name": "large.schema", "inputSchema": schema},
        set(),
    )
    assert metadata["input_schema"] == {"type": "object"}


def test_external_mcp_reads_one_live_input_schema_without_calling_a_tool(
    mcp_setup: tuple[IntegrationMCPAdapter, IntegrationSDKFactory, ClientAuthorizationService, MockMCPHTTP],
) -> None:
    adapter, factory, clients, http = mcp_setup
    sdk = factory.create(caller=caller(clients), integration_id="lynewright.integration.mcp-mock", connection_id="primary")

    schema = adapter.tool_input_schema(sdk, tool_name="approved.echo")

    assert schema == {"type": "object"}
    methods = [call["json_body"].get("method") for call in http.calls]
    assert "tools/list" in methods
    assert "tools/call" not in methods


def test_external_mcp_only_calls_approved_tools(mcp_setup: tuple[IntegrationMCPAdapter, IntegrationSDKFactory, ClientAuthorizationService, MockMCPHTTP]) -> None:
    adapter, factory, clients, http = mcp_setup
    sdk = factory.create(caller=caller(clients), integration_id="lynewright.integration.mcp-mock", connection_id="primary")

    assert adapter.call_tool(sdk, tool_name="approved.echo", arguments={"message": "hello"}) == {"ok": True}
    assert any(call["json_body"].get("method") == "tools/call" for call in http.calls)
    with pytest.raises(PlatformOperationError, match="not approved"):
        adapter.call_tool(sdk, tool_name="unapproved.read")


def test_external_mcp_uses_sdk_network_policy(mcp_setup: tuple[IntegrationMCPAdapter, IntegrationSDKFactory, ClientAuthorizationService, MockMCPHTTP]) -> None:
    adapter, factory, clients, _ = mcp_setup
    sdk = factory.create(caller=caller(clients), integration_id="lynewright.integration.mcp-mock", connection_id="primary")
    sdk._http.validate_public_url = lambda url: (_ for _ in ()).throw(PlatformOperationError("blocked"))  # type: ignore[method-assign]

    with pytest.raises(PlatformOperationError, match="blocked"):
        adapter.discover(sdk)
